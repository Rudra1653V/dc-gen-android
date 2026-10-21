"""dc-gen-android :: orchestrator.

Reads config.toml, brings up the configured emulators, and creates the requested
number of accounts - one worker thread per emulator, accounts distributed
round-robin. The captcha is solved by YOU: the worker beeps, parks, and carries
on the moment the wrapper disappears.

    python main.py                       # use config.toml
    python main.py --instances 2 --accounts 10
    python main.py --dry-run             # show the plan, touch nothing
    python main.py --fresh --emails 3
"""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from core.accounts import AccountFactory, AccountWriter
from core.avd import ensure_emulators
from core.config import ConfigError, load
from core.proxy import apply as apply_proxy
from core.proxy import load as load_proxies
from core.ui import Device, UiError, load_selectors, serial_for
from flows.register import Signup

STOP = threading.Event()
LOG_LOCK = threading.Lock()


def make_log(prefix=""):
    def log(msg=""):
        with LOG_LOCK:
            line = f"{prefix}{msg}" if prefix else str(msg)
            print(line, flush=True)
    return log


def worker(cfg, sel, instance, slots, factory, writer, proxies, results):
    serial = serial_for(instance, cfg.emulator.base_port)
    prefix = f"[{serial}] " if cfg.logging.prefix_serial else f"[emu{instance}] "
    log = make_log(prefix)

    dev = Device(serial)
    try:
        dev.ensure_online(boot_timeout=cfg.emulator.boot_timeout_s)
    except UiError as exc:
        log(f"!! {exc}")
        results[instance] = "offline"
        return

    proxy = proxies[instance % len(proxies)] if proxies else None
    apply_proxy(dev, proxy, log)

    failures = 0
    for slot_index in slots:
        if STOP.is_set():
            break

        acct = factory.make(slot_index, instance)
        acct.started_at = time.strftime("%H:%M:%S")
        log("")
        log(f"=== account {acct.index}/{cfg.run.accounts}  {acct.email}")
        log(f"    name={acct.display_name!r} username={acct.username!r} dob={acct.dob_text}")

        try:
            if cfg.flow.fresh_per_account:
                dev.pm_clear()
            dev.start_app()
            outcome = Signup(dev, sel, acct, log=log, cfg=cfg).run(
                captcha_mode=cfg.captcha.mode,
                human_timeout=cfg.captcha.human_timeout_s,
                max_rounds=cfg.captcha.auto_rounds,
            )
            acct.status = "ok"
            acct.note = outcome
            failures = 0
            log(f"    OK  ({outcome})")
        except UiError as exc:
            acct.status = "fail"
            acct.note = str(exc)
            failures += 1
            log(f"    FAIL {exc}")
        except Exception as exc:                      # noqa: BLE001
            acct.status = "error"
            acct.note = repr(exc)
            failures += 1
            log(f"    ERROR {exc!r}")

        acct.finished_at = time.strftime("%H:%M:%S")
        writer.write(acct)
        factory.commit()
        results[acct.index] = acct.status

        if failures >= cfg.run.max_consecutive_failures:
            log(f"    {failures} consecutive failures - pausing this emulator")
            break
        if not STOP.is_set() and cfg.run.cooldown_s:
            time.sleep(cfg.run.cooldown_s)

    log("worker done")
    results[instance] = "done"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--instances", type=int, help="override [run] emulators")
    ap.add_argument("--accounts", type=int, help="override [run] accounts")
    ap.add_argument("--emails", type=int, help="alias for --accounts")
    ap.add_argument("--captcha-mode", choices=["human", "auto", "skip"],
                    help="override [captcha] mode")
    ap.add_argument("--fresh", action="store_true", help="pm clear before each account")
    ap.add_argument("--no-boot", action="store_true", help="do not boot emulators")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    overrides = {}
    if args.instances:
        overrides["run.emulators"] = args.instances
    n = args.accounts or args.emails
    if n:
        overrides["run.accounts"] = n
    if args.captcha_mode:
        overrides["captcha.mode"] = args.captcha_mode
    if args.fresh:
        overrides["flow.fresh_per_account"] = True
    if args.no_boot:
        overrides["emulator.auto_boot"] = False
    if args.dry_run:
        overrides["run.dry_run"] = True

    try:
        cfg = load(args.config, overrides)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2

    sel = load_selectors()
    factory = AccountFactory(cfg)
    writer = AccountWriter(cfg)
    proxies = load_proxies(cfg)

    print("=" * 68)
    print(f"dc-gen-android  accounts={cfg.run.accounts}  emulators={cfg.run.emulators}")
    print(f"captcha={cfg.captcha.mode}  fresh={cfg.flow.fresh_per_account}  "
          f"output={cfg.output.dir} ({cfg.output.format})")
    print("=" * 68)
    for i in range(cfg.run.emulators):
        print(f"  instance {i}: {cfg.avd_name(i)} @ {serial_for(i, cfg.emulator.base_port)}")
    plan = [factory.make(idx, inst) for idx, inst in cfg.slots()]
    for a in plan:
        print(f"  #{a.index:<3} emu{a.instance}  {a.email:<34} "
              f"{a.display_name:<18} {a.username}")
    print("=" * 68)
    if cfg.run.dry_run:
        return 0

    if cfg.emulator.auto_boot:
        ensure_emulators(cfg)

    results = {}
    threads = []
    for i in range(cfg.run.emulators):
        slots = [idx for idx, inst in cfg.slots() if inst == i]
        t = threading.Thread(target=worker, name=f"emu{i}", daemon=True,
                             args=(cfg, sel, i, slots, factory, writer, proxies, results))
        t.start()
        threads.append(t)

    def on_sigint(*_):
        if STOP.is_set():
            print("\nsecond ^C - hard exit")
            sys.exit(130)
        print("\n^C - finishing current accounts (^C again to force)")
        STOP.set()

    signal.signal(signal.SIGINT, on_sigint)

    while any(t.is_alive() for t in threads):
        time.sleep(0.5)

    ok = sum(1 for v in results.values() if v == "ok")
    print("")
    print("=" * 68)
    print(f"done: {ok} ok, {sum(1 for v in results.values() if v == 'fail')} failed, "
          f"{sum(1 for v in results.values() if v == 'error')} errored")
    print(f"records : {writer.csv_path}")
    print(f"creds   : {writer.cred_path}")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())