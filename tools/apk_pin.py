"""Pin the Discord APK by sha256. Handles plain .apk and .apkm/.xapk bundles.

Version reading tries apkanalyzer (ships with cmdline-tools), then aapt2, then
aapt - cmdline-tools does NOT link aapt, so a bare `aapt` call fails clean.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from core.log import log

BUNDLES = (".apkm", ".xapk", ".apks", ".zip")

def _pick_base(td: str) -> Path | None:
    """XAPK names the base '<package>.apk', not 'base.apk'. Read manifest.json
    if present, else fall back to name, then to size."""
    import json as _json
    mj = Path(td) / "manifest.json"
    if mj.exists():
        try:
            for entry in _json.loads(mj.read_text()).get("split_apks", []):
                if entry.get("id") == "base":
                    cand = Path(td) / entry["file"]
                    if cand.exists():
                        return cand
        except Exception:
            pass
    apks = sorted(Path(td).rglob("*.apk"))
    if not apks:
        return None
    for a in apks:
        if a.name.lower().startswith("base"):
            return a
    for a in apks:
        if a.name.lower().startswith("com.discord"):
            return a
    return max(apks, key=lambda p: p.stat().st_size)


def is_bundle(path: Path) -> bool:
    """Content-based: a bundle is a zip of APKs with no top-level manifest.
    Extension alone is not enough - APKPure serves bundles named '.apk'."""
    if path.suffix.lower() in BUNDLES:
        return True
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except zipfile.BadZipFile:
        return False
    if "AndroidManifest.xml" in names:
        return False
    return any(n.endswith(".apk") for n in names)


@contextlib.contextmanager
def _real_apk(apk: Path):
    if is_bundle(apk):
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(apk) as z:
                z.extractall(td)
            cands = sorted(Path(td).rglob("*.apk"))
            base = _pick_base(td)
            if base is None:
                raise SystemExit(f"no .apk inside {apk.name}")
            yield base
    else:
        yield apk


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _try(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def read_version(apk: Path) -> dict:
    with _real_apk(apk) as real:
        if (exe := shutil.which("apkanalyzer")):
            v = _try([exe, "manifest", "version-name", str(real)]).strip()
            c = _try([exe, "manifest", "version-code", str(real)]).strip()
            if v:
                return {"version": v.splitlines()[-1].strip(),
                        "build_number": int(c) if c.isdigit() else 0}
        for tool in ("aapt2", "aapt"):
            if (exe := shutil.which(tool)):
                out = _try([exe, "dump", "badging", str(real)])
                v = re.search(r"versionName='([^']+)'", out)
                c = re.search(r"versionCode='(\d+)'", out)
                if v:
                    return {"version": v.group(1),
                            "build_number": int(c.group(1)) if c else 0}
    log("could not read the APK version - stamping 0.0.0", "warn")
    return {"version": "0.0.0", "build_number": 0}


def native_archs(apk: Path) -> list[str]:
    try:
        with zipfile.ZipFile(apk) as z:
            return sorted({n.split("/")[1] for n in z.namelist()
                           if n.startswith("lib/") and n.count("/") > 1})
    except Exception:
        return []


def pin(apk: Path, manifest: Path) -> dict:
    info = read_version(apk)
    rec = {"apk": str(apk), "sha256": sha256(apk),
           "bundle": apk.suffix.lower() in BUNDLES, **info}
    manifest.write_text(json.dumps(rec, indent=2))
    log(f"pinned {apk.name} v{rec['version']} build {rec['build_number']} "
        f"({rec['sha256'][:12]})", "ok")
    archs = native_archs(apk)
    if archs:
        log(f"native libs: {', '.join(archs)}")
    return rec


def check(apk: Path, manifest: Path) -> bool:
    if not manifest.exists():
        return False
    rec = json.loads(manifest.read_text())
    got = sha256(apk)
    if got != rec["sha256"]:
        log(f"APK sha mismatch: have {got[:12]}, pinned {rec['sha256'][:12]}", "err")
        return False
    return True