"""Dump the current screen of an instance: hierarchy + png + a text summary.

    .venv/bin/python -m tools.snap 0 register_form
"""
from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

from core import config as cfg_mod
from core.log import log


def main() -> int:
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    label = sys.argv[2] if len(sys.argv) > 2 else "manual"
    cfg = cfg_mod.load()
    serial = f"emulator-{cfg.emulator.console_port_base + 2 * idx}"

    def adb(*a, binary: bool = False) -> bytes:
        return subprocess.run(["adb", "-s", serial, *a],
                              capture_output=True, check=False).stdout

    if adb("shell", "getprop sys.boot_completed").decode().strip() != "1":
        sys.exit(f"{serial} is not up - boot it, or use: "
                 f".venv/bin/python -m tools.walk {idx}")

    raw = adb("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty").decode()
    start, end = raw.find("<"), raw.rfind(">")
    xml = raw[start:end + 1] if start >= 0 and end > start else ""

    out = cfg.paths.shots / f"{datetime.now():%Y%m%d-%H%M%S}_{idx}_{label}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dump_raw.txt").write_text(raw)
    if xml:
        (out / "hierarchy.xml").write_text(xml)
    (out / "screen.png").write_bytes(adb("exec-out", "screencap", "-p", binary=True))

    if xml:
        for n in ET.fromstring(xml).iter("node"):
            t = (n.get("text") or "") + (n.get("content-desc") or "")
            if not t.strip():
                continue
            cls = n.get("class") or ""
            b = n.get("bounds") or ""
            try:
                x1, y1, x2, y2 = map(int, b.strip("[]").replace("][", ",").split(","))
                print(f"{cls:30} centre=({(x1 + x2) // 2},{(y1 + y2) // 2})  text={t!r}")
            except ValueError:
                print(f"{cls:30} bounds={b}  text={t!r}")
    else:
        print("no parseable xml - see dump_raw.txt")
    log(f"snap -> {out}", "ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())