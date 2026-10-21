"""Emulator lifecycle: list, boot, wait, kill."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from core.ui import ADB, EMULATOR, UiError, serial_for

ANDROID_HOME = os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Library/Android/sdk")


def list_avds() -> list:
    p = subprocess.run([EMULATOR, "-list-avds"], capture_output=True)
    return [ln.strip() for ln in p.stdout.decode().splitlines() if ln.strip()]


def attached() -> dict:
    """serial -> state, from `adb devices`."""
    p = subprocess.run([ADB, "devices"], capture_output=True)
    out = {}
    for line in p.stdout.decode().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def is_attached(serial: str) -> bool:
    return attached().get(serial) == "device"


def boot(avd: str, port: int, cfg, log=print):
    if not Path(EMULATOR).exists():
        raise UiError("E_EMULATOR_MISSING", EMULATOR)
    args = [EMULATOR, "-avd", avd, "-port", str(port)]
    if cfg.emulator.no_snapshot_load:
        args.append("-no-snapshot-load")
    if cfg.emulator.headless:
        args.append("-no-window")
    args += ["-memory", str(cfg.emulator.ram_mb)]
    args += list(cfg.emulator.extra_args or [])
    log(f"   boot {avd} on port {port}: {' '.join(args[1:])}")
    log_path = Path(cfg.artifacts.dir) / f"{avd}.emu.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as fh:
        subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return wait_boot(serial_for(int((port - cfg.emulator.base_port) // 2),
                                cfg.emulator.base_port),
                     cfg.emulator.boot_timeout_s, log)


def wait_boot(serial: str, timeout: int, log=print) -> bool:
    subprocess.run([ADB, "-s", serial, "wait-for-device"], capture_output=True,
                   timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = subprocess.run([ADB, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                           capture_output=True)
        if p.stdout.decode().strip() == "1":
            log(f"   {serial} booted")
            return True
        time.sleep(2)
    raise UiError("E_DEVICE_OFFLINE", f"{serial} never finished booting")


def ensure_emulators(cfg, log=print) -> list:
    """Make sure every configured instance is attached and booted."""
    serials = []
    avds = set(list_avds())
    for i in range(cfg.run.emulators):
        serial = serial_for(i, cfg.emulator.base_port)
        avd = cfg.avd_name(i)
        if is_attached(serial):
            log(f"   {serial} already attached")
            serials.append(serial)
            continue
        if not cfg.emulator.auto_boot:
            raise UiError("E_DEVICE_OFFLINE", f"{serial} not attached and auto_boot is off")
        if avd not in avds:
            raise UiError("E_AVD_MISSING",
                          f"AVD {avd!r} does not exist (have: {sorted(avds)})")
        boot(avd, cfg.emulator.base_port + 2 * i, cfg, log)
        serials.append(serial)
    return serials


def kill(serial: str, log=print):
    log(f"   killing {serial}")
    subprocess.run([ADB, "-s", serial, "emu", "kill"], capture_output=True)