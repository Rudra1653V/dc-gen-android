"""Batch model: N accounts -> all park -> the human verifies N in one sitting ->
all finalize -> all clear -> repeat.

finalize() opens every gate up front, then polls in parallel.
"""
from __future__ import annotations

import queue
import threading
import time

from core.context import Ctx
from core.errors import DCGenError, FailureCode, HumanAbort
from core.human import HumanManager
from core.log import bind_instance, log, unbind
from core.state import Account
from device import selectors as sel_mod
from flows import email_verify, register

STAGGER_S = 5.0
ATTEMPTS = 2
RATE_FLOOR = 15.0


def _preflight(cfg, backend) -> object:
    book = sel_mod.load(cfg.paths.selectors)
    problems = sel_mod.validate(book)
    if problems:
        for p in problems:
            log(p, "err")
        raise SystemExit("selectors.yaml is inconsistent")
    stale = book.validate_unverified()
    if stale:
        log(f"{len(stale)} meta.unverified_ids entries are not in selectors.yaml: "
            f"{stale[:4]}", "warn")
    for inst in cfg.run.instances:
        if not any(i.idx == inst for i in backend.list_instances()):
            raise SystemExit(f"dcgen-{inst} does not exist - run --setup first")
    snap = (backend.avd_home / f"{cfg.emulator.prefix}{cfg.run.instances[0]}.avd"
            / "snapshots" / cfg.emulator.snapshot)
    if not snap.exists():
        raise SystemExit(f"no '{cfg.emulator.snapshot}' snapshot - run --setup first")
    return book


def _boot(cfg, backend, book):
    from core import mitm
    from core.proxy import Forwarders, load_proxies
    from device import identity as ident_mod

    ident_mod.assert_distinct([ident_mod.for_instance(i) for i in cfg.run.instances])

    fwd = None
    if not cfg.net.no_proxy:
        proxies = load_proxies(cfg.net.proxy_file)
        if proxies:
            fwd = Forwarders(proxies, cfg.net.proxy_mode, cfg.net.dead_cooldown,
                             cfg.net.handshake_timeout)
            for idx in cfg.run.instances:
                port = backend.instance(idx).proxy_port
                if cfg.net.mitm:
                    mitm.start(cfg, idx, port, fwd, cfg.paths.logs)
                else:
                    fwd.ensure(port)
                backend.set_proxy(idx, f"127.0.0.1:{port}")
            time.sleep(1.0)
            log(f"{len(proxies)} proxies loaded, {len(cfg.run.instances)} forwarders up")
        else:
            log(f"{cfg.net.proxy_file} has no usable proxies - going direct", "warn")

    for idx in cfg.run.instances:
        backend.start(idx, headless=backend.headless)
    for idx in cfg.run.instances:
        backend.wait_booted(idx)
        backend.probe_props(idx)
        backend.randomize_identity(idx)
        if fwd is not None:
            backend.set_proxy_runtime(
                idx, f"127.0.0.1:{backend.instance(idx).proxy_port}")

    if cfg.net.mitm:
        from tools import ca_install
        for idx in cfg.run.instances:
            ca_install.ensure(backend, idx)
    return fwd


def _assert_egress(cfg, ctx) -> None:
    import requests
    try:
        host_ip = requests.get("https://api.ipify.org", timeout=15).text.strip()
    except Exception:
        host_ip = ""

    seen: dict[str, int] = {}
    for idx in cfg.run.instances:
        ip = ctx.device(idx).egress_ip()
        if not ip:
            log(f"inst-{idx} egress check failed (no curl/wget in the guest)", "warn")
            continue
        if ip == host_ip:
            log(f"inst-{idx} exits on the HOST ip {ip} - proxy not in use", "warn")
        if ip in seen:
            log(f"inst-{idx} shares exit IP {ip} with inst-{seen[ip]}", "warn")
        seen[ip] = idx
        log(f"inst-{idx} exit IP {ip}")

    for idx in cfg.run.instances:
        if not ctx.device(idx).gateway_ok():
            log(f"inst-{idx} cannot reach discord.com/api/v9/gateway - check the "
                f"forwarder before trusting this run", "err")


def _one(cfg, ctx, idx: int) -> Account | None:
    token = bind_instance(idx)              # <-- keep the token
    try:
        acct = register.run(ctx, idx)
        return acct
    except HumanAbort:
        return None
    except DCGenError as exc:
        log(f"failed: {exc.code.value} {exc}", "err")
        return None
    except SystemExit as exc:               # library code raises this, not Exception
        log(f"failed: {exc}", "err")
        return None
    except Exception as exc:
        log(f"unexpected: {type(exc).__name__}: {exc}", "err")
        return None
    finally:
        unbind(token)                       # <-- not None


def _round(cfg, ctx) -> list[Account]:
    q: queue.Queue = queue.Queue()
    for i, idx in enumerate(cfg.run.instances):
        q.put((idx, i * STAGGER_S))
    results: list[Account] = []
    lock = threading.Lock()

    def worker():
        while True:
            try:
                idx, delay = q.get_nowait()
            except queue.Empty:
                return
            try:
                if delay:
                    time.sleep(delay)
                if cfg.run.rotate_identity:
                    ctx.backend.randomize_identity(idx)
                acct = _one(cfg, ctx, idx)
                if acct:
                    with lock:
                        results.append(acct)
            finally:
                q.task_done()

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(cfg.concurrency, len(cfg.run.instances)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def finalize(cfg, backend, ctx, store, accounts: list[Account]) -> int:
    if not accounts:
        return 0

    gate_errors: dict[str, Exception] = {}
    glock = threading.Lock()

    def open_gate(acct):
        try:
            email_verify.gate(ctx, acct)
        except HumanAbort:
            pass
        except Exception as exc:
            with glock:
                gate_errors[acct.email] = exc

    threads = [threading.Thread(target=open_gate, args=(a,), daemon=True)
               for a in accounts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    promotable = [a for a in accounts if a.email not in gate_errors]
    results = email_verify.poll_many(cfg, promotable)

    promoted = 0
    for acct in accounts:
        if acct.email in gate_errors:
            store.park(acct, "pending")
            log(f"{acct.email} gate error: {gate_errors[acct.email]}", "warn")
            continue
        me = results.get(acct.email)
        if not me:
            store.park(acct, "pending")
            log(f"{acct.email} still unverified after {cfg.email.verify_timeout}s",
                "warn")
            continue
        email_verify.promote(ctx, acct, me)
        if acct.state.value == "DONE":
            promoted += 1
    return promoted


def run_batches(cfg, backend, store) -> int:
    book = _preflight(cfg, backend)
    human = HumanManager(cfg, backend)
    human.start()
    ctx = Ctx(cfg=cfg, backend=backend, store=store, human=human, book=book)
    made = failed = 0
    fwd = None

    try:
        fwd = _boot(cfg, backend, book)
        if not cfg.net.no_proxy:
            _assert_egress(cfg, ctx)

        batch_size = cfg.run.batch or len(cfg.run.instances)
        round_no = 0
        while (not human.aborted
               and (cfg.run.infinite or round_no * batch_size < cfg.run.count)):
            round_no += 1
            log(f"=== batch {round_no} ({batch_size} account(s)) ===", "ok")
            results = _round(cfg, ctx)

            if cfg.run.dry_run:
                log("dry run complete - nothing written", "ok")
                return 0

            if not results:
                log("nobody reached the pending state", "err")
                failed += batch_size
                continue

            log(f"{len(results)} parked - hand over to the human", "warn")
            promoted = finalize(cfg, backend, ctx, store, results)
            made += promoted
            failed += len(results) - promoted

            for idx in cfg.run.instances:
                backend.app_clear(idx)

        log(f"done - {made} verified, {failed} failed", "ok")
        for stale in store.stale("pending"):
            log(f"pending > 24h: {stale.email} (Discord reaps unverified accounts)",
                "warn")
        return 0
    finally:
        human.stop()
        if fwd:
            fwd.stop_all()
        from core import mitm
        mitm.stop_all()
        if cfg.run.report:
            store.write_report()
        if not cfg.run.keep_open:
            backend.stop_all()


def _retry_locked(cfg, ctx, accounts: list[Account]) -> None:
    if not cfg.sms.enabled:
        return
    locked = {a.email for a in ctx.store.read_queue("locked")}
    for acct in accounts:
        if acct.email not in locked:
            continue
        from flows import phone_wall
        log(f"retrying the phone wall for {acct.email}")
        phone_wall.run(ctx, acct)


def finalize_pending(cfg, backend, store) -> int:
    book = sel_mod.load(cfg.paths.selectors)
    pending = store.read_queue("pending")
    if not pending:
        log("pending.txt is empty", "ok")
        return 0
    log(f"{len(pending)} account(s) in pending.txt")
    human = HumanManager(cfg, backend)
    human.start()
    ctx = Ctx(cfg=cfg, backend=backend, store=store, human=human, book=book)
    fwd = None
    try:
        fwd = _boot(cfg, backend, book)
        _retry_locked(cfg, ctx, pending)
        promoted = finalize(cfg, backend, ctx, store, pending)
        remaining = store.read_queue("pending")
        log(f"promoted {promoted}, {len(remaining)} still pending",
            "ok" if promoted else "warn")
        for stale in store.stale("pending"):
            log(f"pending > 24h: {stale.email}", "warn")
        left_locked = store.read_queue("locked")
        if left_locked:
            log(f"{len(left_locked)} account(s) still phone-locked in locked.txt")
        return 0
    finally:
        human.stop()
        if fwd:
            fwd.stop_all()
        from core import mitm
        mitm.stop_all()
        if cfg.run.report:
            store.write_report()
        backend.stop_all()


def resume(cfg, backend, store) -> int:
    pending = store.read_queue("pending")
    if not pending:
        log("nothing to resume - starting fresh", "warn")
        return run_batches(cfg, backend, store)
    for acct in pending:
        log(f"resuming {acct.email} (inst-{acct.instance}, state {acct.state.value})")
    return finalize_pending(cfg, backend, store)