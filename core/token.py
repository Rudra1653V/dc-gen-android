"""Token capture and validation. Order fixed: prefs -> API login -> MITM.

A 403 is treated as "valid but limited" rather than invalid, because Discord
403s some endpoints for unverified tokens and throwing a good token away is
worse than keeping it.
"""
from __future__ import annotations

import re

from core.errors import DCGenError, FailureCode
from core.http import request
from core.log import log

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,7}\.[A-Za-z0-9_-]{25,}")
NON_TOKENS = {"null", "undefined"}
LIMITED_STATUS = (401, 403)


def extract(blob: str) -> list[str]:
    if not blob:
        return []
    out = []
    for hit in TOKEN_RE.findall(blob):
        if hit.lower() not in NON_TOKENS and hit not in out:
            out.append(hit)
    return out


def looks_like(value: str) -> bool:
    return bool(value) and bool(TOKEN_RE.fullmatch(value.strip()))


def _login(cfg, email: str, password: str, session=None) -> dict | None:
    r = request(cfg, "POST", "/auth/login", attempts=2, session=session,
                json_body={"login": email, "password": password, "undelete": "false"})
    if r.status_code == 200:
        return r.json()
    if "captcha" in r.text.lower():
        log("api login wants a captcha", "warn")
    else:
        log(f"api login -> {r.status_code}: {r.text[:160]}", "warn")
    return None


def api_login(cfg, email: str, password: str, session=None) -> str | None:
    j = _login(cfg, email, password, session)
    return (j or {}).get("token")


def validate(cfg, token: str, session=None, *, allow_limited: bool = True) -> dict | None:
    if not looks_like(token):
        return None
    try:
        r = request(cfg, "GET", "/users/@me", token=token, attempts=2, session=session)
    except DCGenError:
        return None
    if r.status_code == 200:
        return r.json()
    if allow_limited and r.status_code in LIMITED_STATUS:
        log(f"/users/@me -> {r.status_code}; treating token as valid-but-limited", "warn")
        return {"id": "", "verified": False, "_limited": True}
    return None


def fill_identity(cfg, me: dict, email: str, password: str, session=None) -> dict:
    if me.get("id") or not me.get("_limited"):
        return me
    j = _login(cfg, email, password, session)
    if j:
        user = j.get("user") or {}
        me["id"] = str(user.get("id", ""))
        me["verified"] = bool(user.get("verified", False))
    return me


def usability(cfg, token: str, session=None) -> dict:
    out = {"me": False, "gateway": False, "phone": None, "flags": None}
    me = validate(cfg, token, session, allow_limited=True)
    if me:
        out["me"] = True
        out["phone"] = me.get("phone")
        out["flags"] = me.get("flags")
    try:
        r = request(cfg, "GET", "/gateway", token=token, attempts=1, session=session)
        out["gateway"] = r.status_code == 200
    except DCGenError:
        pass
    return out


def capture(cfg, device, email: str, password: str, *, session=None) -> tuple[str, dict]:
    attempts: list[tuple[str, str | None]] = []
    if cfg.net.mitm:
        from core.mitm import last_token
        attempts.append(("mitm", last_token(device.idx)))
    attempts.append(("prefs", device.prefs_token()))
    attempts.append(("api_login", api_login(cfg, email, password, session)))
    attempts.append(("prefs-retry", device.prefs_token()))

    for source, token in attempts:
        if not token:
            log(f"token source {source}: nothing", "warn")
            continue
        me = validate(cfg, token, session)
        if me:
            me = fill_identity(cfg, me, email, password, session)
            log(f"token captured via {source} (id {me.get('id') or 'unknown'})", "ok")
            return token, me
        log(f"token source {source} produced an invalid token", "warn")

    raise DCGenError(FailureCode.E_NO_TOKEN,
                     f"no valid token from prefs/api_login for {email}")