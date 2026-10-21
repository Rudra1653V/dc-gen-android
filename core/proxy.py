"""Per-device HTTP proxy via the global settings, and the stale-proxy fix.

`settings put global http_proxy` persists across reboots and across accounts, so
going direct again REQUIRES deleting it - writing an empty value is not enough.
"""
from __future__ import annotations

from pathlib import Path


def parse(line: str):
    """host:port or host:port:user:pass -> (host, port, user, password)."""
    parts = line.strip().split(":")
    if len(parts) == 2:
        return parts[0], parts[1], None, None
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], parts[3]
    raise ValueError(f"bad proxy line: {line!r}")


def load(cfg):
    if not cfg.proxy.enabled:
        return []
    lines = Path(cfg.proxy.file).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def apply(dev, proxy: str | None, log=print):
    if not proxy:
        log("   proxy -> direct (deleting the global setting)")
        dev.shell("settings delete global http_proxy", check=False)
        dev.shell("settings delete global global_http_proxy_host", check=False)
        dev.shell("settings delete global global_http_proxy_port", check=False)
        return
    host, port, user, password = parse(proxy)
    log(f"   proxy -> {host}:{port}")
    dev.shell(f"settings put global http_proxy {host}:{port}", check=False)
    if user:
        dev.shell(f"settings put global global_http_proxy_username {user}", check=False)
        dev.shell(f"settings put global global_http_proxy_password {password}", check=False)