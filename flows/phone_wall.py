"""Optional phone wall. API first. Owns the locked.txt bookkeeping."""
from __future__ import annotations

from core import sms
from core.http import request
from core.log import log
from core.state import State


def _release(cfg, token: str, password: str) -> bool:
    for payload in ({"password": password}, None):
        r = request(cfg, "DELETE", "/users/@me/phone", token=token, json_body=payload)
        if r.status_code in (200, 204):
            return True
    return False


def _attach(cfg, token: str, password: str, e164: str, code: str) -> bool:
    send = request(cfg, "POST", "/phone-verifications/send", token=token,
                   json_body={"phone": e164})
    if send.status_code not in (200, 201):
        return False
    pt = (send.json() or {}).get("phone_token")
    if not pt:
        return False
    ver = request(cfg, "POST", "/phone-verifications/verify", token=token,
                  json_body={"phone_token": pt, "code": code})
    if ver.status_code != 200:
        return False
    pt2 = (ver.json() or {}).get("phone_token")
    if not pt2:
        return False
    for payload in ({"phone_token": pt2, "password": password}, {"phone_token": pt2}):
        r = request(cfg, "POST", "/users/@me/phone", token=token, json_body=payload)
        if r.status_code in (200, 204):
            return True
    return False


def run(ctx, acct) -> bool:
    cfg = ctx.cfg
    if not cfg.sms.enabled or not (cfg.sms.provider and cfg.sms.key):
        return True

    for attempt in range(1, cfg.sms.retries + 1):
        order = None
        try:
            e164, order = sms.send(cfg.sms.provider, cfg.sms.key,
                                   cfg.sms.country, cfg.sms.max_price or None)
            log(f"[{attempt}/{cfg.sms.retries}] rented {e164} (order {order})")
            code = sms.poll(cfg.sms.provider, cfg.sms.key, order)
            if code and _attach(cfg, acct.token, acct.password, e164, code):
                sms.finish(cfg.sms.provider, cfg.sms.key, order)
                if cfg.sms.release_phone:
                    log("number released" if _release(cfg, acct.token, acct.password)
                        else "release failed", "ok")
                ctx.store.drop_from_queue(acct.email, "locked")
                return True
            sms.cancel(cfg.sms.provider, cfg.sms.key, order)
        except Exception as exc:
            log(f"sms attempt {attempt} failed: {exc}", "err")
            if order:
                sms.cancel(cfg.sms.provider, cfg.sms.key, order)

    acct.mark(State.PHONE_LOCKED)
    ctx.store.park(acct, "locked")
    log(f"{acct.email} stays phone-locked", "warn")
    return False