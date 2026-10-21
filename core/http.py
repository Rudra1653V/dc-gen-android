"""Host-side HTTP with mobile Discord headers, retries and jitter."""
from __future__ import annotations

import base64
import json
import random
import time

import requests

from core.errors import DCGenError, FailureCode
from core.log import log

API = "https://discord.com/api/v9"

_tls_client = None
_tls_tried = False


def _probe_session():
    global _tls_client, _tls_tried
    if _tls_tried:
        return _tls_client
    _tls_tried = True
    try:
        import tls_client
        _tls_client = tls_client.Session(client_identifier="chrome_120")
    except Exception as exc:
        log(f"tls_client unavailable ({exc}); using requests", "warn")
        _tls_client = None
    return _tls_client


def super_properties(cfg) -> str:
    payload = {
        "os": "Android",
        "browser": "Discord Android",
        "device": "",
        "system_locale": cfg.device.locale,
        "client_version": cfg.app.version,
        "release_channel": "stable",
        "client_build_number": cfg.app.build_number,
        "os_version": "34",
        "client_event_source": None,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def headers(cfg, token: str | None = None) -> dict:
    h = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": ("Discord-Android/{} (Linux; U; Android 34; en_US; "
                       "Pixel 7; Build/TQ3A.230805.001)").format(cfg.app.version),
        "X-Discord-Locale": cfg.device.locale,
        "X-Super-Properties": super_properties(cfg),
        "Origin": "https://discord.com",
    }
    if token:
        h["Authorization"] = token
    return h


def request(cfg, method: str, path: str, *, token: str | None = None,
            json_body: dict | None = None, attempts: int = 3,
            timeout: int = 30, session=None):
    url = path if path.startswith("http") else f"{API}{path}"
    sess = session or requests
    last = None
    for i in range(1, attempts + 1):
        try:
            r = sess.request(method, url, headers=headers(cfg, token),
                             json=json_body, timeout=timeout)
            if r.status_code in (429, 500, 502, 503):
                wait = float((r.json() or {}).get("retry_after", 1)) if r.content else 1.0
                wait += random.uniform(0, 0.5)
                log(f"{method} {path} -> {r.status_code}, sleeping {wait:.1f}s", "warn")
                time.sleep(wait)
                last = r
                continue
            return r
        except requests.RequestException as exc:
            last = exc
            back = min(2 ** i, 15) + random.uniform(0, 1)
            log(f"{method} {path} failed ({exc}); retry in {back:.1f}s", "warn")
            time.sleep(back)
    if isinstance(last, Exception):
        raise DCGenError(FailureCode.E_NETWORK, f"{method} {path}: {last}")
    return last


def ratelimit_probe(cfg) -> float:
    """Ported from the original preflight_ratelimit(): returns seconds to wait."""
    sess = _probe_session()
    body = {
        "email": f"probe{random.randint(100000, 999999)}@example.com",
        "password": "Ultimate12$$",
        "date_of_birth": "2000-09-20",
        "username": f"probe{random.randint(100000, 999999)}",
        "consent": True,
        "captcha_service": "hcaptcha",
        "global_name": "probe",
        "captcha_key": None,
        "invite": None,
        "promotional_email_opt_in": False,
        "gift_code_sku_id": None,
    }
    try:
        if sess is not None:
            sess.headers = headers(cfg)
            r = sess.post(f"{API}/auth/register", json=body)
            text = r.text
        else:
            r = requests.post(f"{API}/auth/register", json=body, headers=headers(cfg),
                              timeout=30)
            text = r.text
        if "rate limited" in text:
            return float(r.json().get("retry_after", 1))
    except Exception as exc:
        log(f"ratelimit probe failed: {exc}", "warn")
    return 1.0