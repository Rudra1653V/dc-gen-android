"""M0: boot one AVD, install the pinned APK, dump every onboarding screen."""
from __future__ import annotations

from datetime import datetime

from core.log import log

SCREENS = [
    ("launcher",    "tap the Discord icon from the launcher"),
    ("onboarding",  "advance the onboarding carousel to the last page"),
    ("register",    "open the register form"),
    ("form_filled", "fill the form but do not submit"),
    ("captcha",     "submit and catch the captcha webview"),
    ("pending",     "the 'verify your email' state"),
    ("logged_in",   "after verification"),
]


def _dump(backend, idx: int, out, state: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "screen.png").write_bytes(
        backend.adb_run(idx, "exec-out", "screencap", "-p", binary=True))
    backend.adb_run(idx, "shell", "rm -f /sdcard/window_dump.xml", check=False)
    backend.adb_run(idx, "shell", "uiautomator dump /sdcard/window_dump.xml",
                    check=False, timeout=45)
    (out / "hierarchy.xml").write_text(
        backend.adb_run(idx, "shell", "cat /sdcard/window_dump.xml", check=False),
        encoding="utf-8")
    log(f"captured {state} -> {out}")


def run(cfg, backend, idx: int = 0, interactive: bool = True) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backend.start(idx, headless=False)
    backend.wait_booted(idx)
    if cfg.app.apk:
        backend.install_apk(idx, cfg.app.apk)
    backend.window(idx)
    log("recon: scrcpy window is open - drive the emulator by hand")
    for state, hint in SCREENS:
        if interactive:
            input(f"\n  [{state}] {hint}\n  press Enter to capture... ")
        _dump(backend, idx, cfg.paths.shots / f"{stamp}_{idx}_{state}", state)
    log("recon done - reconcile meta.unverified_ids against the dumps, then empty "
        "the list", "ok")