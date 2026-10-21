"""EMAIL_VERIFY_PENDING -> VERIFIED. gate() and poll_many() are split so the
orchestrator can open every gate up front, then poll in parallel."""
from __future__ import annotations

import threading
import time

from core import email as email_mod
from core import token as token_mod
from core.errors import DCGenError, FailureCode
from core.log import log
from core.state import State

POLL = 10


def deliver(ui, device, payload: str) -> str:
    payload = payload.strip()
    if not payload:
        return "none"
    if payload.startswith("http"):
        device.open_url(payload, package=None)
        time.sleep(2)
        if ui.exists("intent_chooser"):
            log("intent chooser appeared - taking the browser entry", "warn")
            ui.tap_if("intent_chooser_browser", timeout=5)
        log("opened the verification link inside the emulator")
        return "link"
    if ui.exists("code_field"):
        ui.type_into("code_field", payload, clear=True)
        device.enter()
        return "code"
    log("no code field - falling back to the API poll", "warn")
    return "none"


def gate(ctx, acct) -> None:
    cfg, idx = ctx.cfg, acct.instance
    email_mod.open_inbox(cfg, acct.email)
    payload = ctx.human.open(
        "email_verify", idx,
        f"open the mail for {acct.email}, then paste the link (or the code) - "
        f"blank when done",
        needs_input=True, timeout=cfg.email.verify_timeout)
    if payload and cfg.email.verify_mode == "paste":
        deliver(ctx.ui(idx), ctx.device(idx), payload)


def _poll_one(cfg, acct, timeout: int) -> dict | None:
    deadline = time.time() + timeout
    last = 0.0
    while time.time() < deadline:
        me = token_mod.validate(cfg, acct.token)
        if me and me.get("verified"):
            return me
        if time.time() - last > 30:
            log(f"[inst-{acct.instance}] {acct.email} still unverified "
                f"({int(deadline - time.time())}s left)", "warn")
            last = time.time()
        time.sleep(POLL)
    return None


def poll_many(cfg, accounts: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    lock = threading.Lock()

    def worker(acct):
        me = _poll_one(cfg, acct, cfg.email.verify_timeout)
        if me:
            with lock:
                out[acct.email] = me

    threads = [threading.Thread(target=worker, args=(a,), daemon=True)
               for a in accounts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def promote(ctx, acct, me: dict) -> None:
    acct.user_id = str(me.get("id", acct.user_id))
    acct.mark(State.VERIFIED)

    usable = token_mod.usability(ctx.cfg, acct.token)
    if not usable["me"]:
        log(f"{acct.email}: token did not survive verification - parking", "warn")
        ctx.store.park(acct, "pending")
        return
    acct.detail = f"gateway={usable['gateway']} phone={usable['phone']}"

    ctx.store.save_evs(acct)
    ctx.store.drop_from_queue(acct.email, "pending")
    ctx.store.drop_from_queue(acct.email, "locked")
    acct.mark(State.DONE)
    ctx.store.save_record(acct)
    log(f"verified and promoted to evs.txt: {acct.email}", "ok")


def run(ctx, acct) -> bool:
    gate(ctx, acct)
    me = _poll_one(ctx.cfg, acct, ctx.cfg.email.verify_timeout)
    if not me:
        raise DCGenError(FailureCode.E_VERIFY_TIMEOUT, f"{acct.email} still unverified")
    promote(ctx, acct, me)
    return True