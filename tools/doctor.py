"""Preflight check. BAD entries mean the run will not work.

    ./run.sh -m tools.doctor
    ./run.sh -m tools.doctor --write-config
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from core.config import CANDIDATES, ConfigError, load, write_default
from core.ui import ADB, ANDROID_HOME, EMULATOR

OK, WARN, BAD = "OK  ", "WARN", "BAD "
results = []


def check(status, label, detail=""):
    results.append((status, label, detail))
    print(f"  [{status}] {label}" + (f" - {detail}" if detail else ""))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-config", action="store_true")
    args = ap.parse_args(argv)

    print("dc-gen-android :: doctor\n")

    v = sys.version_info
    check(OK if v >= (3, 11) else BAD, f"python {v.major}.{v.minor}", "")

    check(OK if Path(ADB).exists() else BAD, "adb", ADB)
    check(OK if Path(EMULATOR).exists() else BAD, "emulator", EMULATOR)

    p = subprocess.run([EMULATOR, "-list-avds"], capture_output=True)
    avds = [ln.strip() for ln in p.stdout.decode().splitlines() if ln.strip()]
    check(OK if avds else BAD, f"AVDs ({len(avds)})", ", ".join(avds) or "none")

    images = Path(ANDROID_HOME) / "system-images"
    n_images = len(list(images.rglob("system.img"))) if images.exists() else 0
    # zero images is BAD, not OK - you cannot create an AVD without one
    check(OK if n_images else BAD, f"system images ({n_images})",
          "" if n_images else "none installed")

    from shutil import which
    for name in ("tesseract", "scrcpy"):
        check(OK if which(name) else WARN, name, "" if which(name) else "not installed")

    apks = list((Path.home() / "Downloads").glob("discord*.xapk"))
    apks += list((Path.home() / "Downloads").glob("discord*.apk"))
    check(OK if apks else WARN, "discord apk",
          str(apks[0]) if apks else "not found in ~/Downloads")

    if args.write_config:
        cfg_path = write_default()
        print(f"\nwrote {cfg_path}")
    existing = next((Path(c) for c in CANDIDATES if Path(c).exists()), None)
    if existing:
        try:
            cfg = load(existing)
            check(OK, str(existing),
                  f"accounts={cfg.run.accounts} emulators={cfg.run.emulators}")
        except ConfigError as exc:
            check(BAD, str(existing), str(exc))
    else:
        check(WARN, "config", "missing - run with --write-config")

    for d in ("state", "accounts", "artifacts"):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            check(OK, f"writable {d}/")
        except OSError as exc:
            check(BAD, f"writable {d}/", str(exc))

    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    print(f"\n{len(results) - len(bad) - len(warn)} ok, {len(warn)} warn, {len(bad)} bad")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())