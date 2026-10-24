#!/usr/bin/env python3
"""Fetch a Discord APK and pin its digest + version. The APK is the source of truth.

    ./run.sh tools/fetch_apk.py                       # official channel, latest stable
    ./run.sh tools/fetch_apk.py --url <direct.apk>    # any direct link
    ./run.sh tools/fetch_apk.py --apk ~/apk/discord.xapk   # (re)pin a local file only
"""
from __future__ import annotations

import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, urllib.request, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.log import banner, log

OFFICIAL = "https://discord.com/api/download?platform=android"
PIN = Path("configs/apk.json")
DEFAULT_DIR = Path.home() / "apk"
UA = "Mozilla/5.0 (Macintosh; Apple Silicon) dcgen-fetch/1"


def _download(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        log(f"GET {url} -> {r.geturl()}")
        total = int(r.headers.get("Content-Length") or 0)
        tmp = dest.with_suffix(dest.suffix + ".part")
        got = 0
        with open(tmp, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk); got += len(chunk)
                if total:
                    print(f"\r  {got/1e6:6.1f}/{total/1e6:.1f} MB", end="", flush=True)
        print()
    os.replace(tmp, dest)
    return dest


def _aapt2() -> str:
    for t in ("aapt2", "aapt"):
        if p := shutil.which(t):
            return p
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk and (bt := Path(sdk) / "build-tools").exists():
        for d in sorted(bt.iterdir(), reverse=True):
            for t in ("aapt2", "aapt"):
                if (d / t).exists():
                    return str(d / t)
    raise SystemExit("need aapt2 (or aapt) on PATH or under $ANDROID_HOME/build-tools")


def _base_apk(path: Path) -> Path:
    """A .xapk/.apks is a zip of splits - badging needs the base entry."""
    if path.suffix.lower() not in (".xapk", ".apks"):
        return path
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(path) as z:
            z.extractall(td)
        apks = list(Path(td).rglob("*.apk"))
        base = next((a for a in apks if a.name in ("base.apk", "com.discord.apk")), None) \
               or max(apks, key=lambda a: a.stat().st_size)
        out = path.with_suffix(".base.apk")
        shutil.copy2(base, out)
        return out


def _version(apk: Path) -> tuple[str, int]:
    tool = _aapt2()
    out = subprocess.run([tool, "dump", "badging", str(apk)],
                         capture_output=True, text=True).stdout
    name = re.search(r"versionName='([^']+)'", out)
    code = re.search(r"versionCode='(\d+)'", out)
    if not (name and code):
        raise SystemExit(f"could not read versionName/versionCode from {apk}")
    return name.group(1), int(code.group(1))


def _pin(path: Path, version: str, code: int, source: str) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    data = json.loads(PIN.read_text()) if PIN.exists() else {}
    data.update({"apk": str(path.resolve()), "sha256": digest,
                 "version": version, "build_number": code,
                 "source_url": source, "fetched_at": __import__("time").strftime(
                     "%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())})
    PIN.parent.mkdir(parents=True, exist_ok=True)
    PIN.write_text(json.dumps(data, indent=2) + "\n")
    log(f"pinned {path.name} v{version} ({code}) sha256={digest[:12]}", "ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=OFFICIAL)
    ap.add_argument("--apk")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    args = ap.parse_args()

    banner("fetch_apk")
    if args.apk:
        apk, source = Path(args.apk).expanduser(), "local"
    else:
        name = "discord.xapk" if args.url.endswith(".xapk") else "discord.apk"
        dest = Path(args.dir).expanduser(); dest.mkdir(parents=True, exist_ok=True)
        apk = _download(args.url, dest / name)
        source = args.url

    version, code = _version(_base_apk(apk))
    _pin(apk, version, code, source)
    log(f"next: ./run.sh dcgen.py setup --apk {apk}", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
