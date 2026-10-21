"""mitmproxy CA into /system/etc/security/cacerts. Only with --mitm."""
from __future__ import annotations

import subprocess
from pathlib import Path

from core.log import log

CA = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"


def _subject_hash_old(pem: Path) -> str:
    out = subprocess.run(
        ["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", str(pem)],
        capture_output=True, text=True)
    return out.stdout.splitlines()[0].strip() if out.stdout else ""


def install(backend, idx: int) -> None:
    if not CA.exists():
        log(f"{CA} missing - run mitmproxy once, then re-run --setup", "warn")
        return
    digest = _subject_hash_old(CA)
    if not digest:
        return
    backend.adb_run(idx, "root", check=False)
    backend.adb_run(idx, "remount", check=False)
    backend.adb_run(idx, "push", str(CA), f"/system/etc/security/cacerts/{digest}.0",
                    timeout=120)
    backend.adb_run(idx, "shell", "chmod", "644",
                    f"/system/etc/security/cacerts/{digest}.0", check=False)
    log(f"CA installed as {digest}.0 on dcgen-{idx}")


def ensure(backend, idx: int) -> None:
    if not CA.exists():
        return
    digest = _subject_hash_old(CA)
    if not digest:
        return
    out = backend.adb_run(idx, "shell", "ls", "/system/etc/security/cacerts", check=False)
    if f"{digest}.0" not in out:
        install(backend, idx)