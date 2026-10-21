#!/usr/bin/env python3
"""dc-gen-android :: v2 entry point.

    ./run.sh dcgen.py setup      # create AVDs, install the APK, snapshot, CA
    ./run.sh dcgen.py doctor     # preflight, no side effects
    ./run.sh dcgen.py recon      # dump the screens, write nothing
    ./run.sh dcgen.py run        # the batch loop
    ./run.sh dcgen.py finalize   # verify everything in pending.txt
    ./run.sh dcgen.py resume     # resume from pending.txt

Batch model: N accounts -> all park -> you verify N in one sitting -> all
finalize -> all clear -> repeat.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import orchestrator
from core.config import ConfigError, load
from core.errors import DCGenError
from core.log import banner, log, start_file, stop_file
from core.state import RunLock, Store
from device import selectors as sel_mod

DEFAULT_CONFIG = "configs/config.yaml"
APK_PIN = Path("configs/apk.json")


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def build_backend(cfg, windowed: bool = False):
    from backends.avd import AvdBackend
    return AvdBackend(cfg, headless=not windowed)


def _caffeinate(enable: bool):
    if not enable or sys.platform != "darwin":
        return None
    try:
        return subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    except FileNotFoundError:
        return None


def _plan(cfg) -> None:
    print("=" * 68)
    print(f"dc-gen-android  count={cfg.run.count}  "
          f"instances={cfg.run.instances}  conc={cfg.concurrency}")
    print(f"captcha={cfg.captcha.mode}  human={cfg.human.mode}  "
          f"mitm={cfg.net.mitm}  out={cfg.paths.out_dir}")
    print("=" * 68)
    for idx, inst in cfg.slots():
        print(f"  #{idx:<3} emu{inst}")
    print("=" * 68)


def _pin_apk(path: Path) -> None:
    """Record the APK path + digest so doctor can prove it has not moved."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    data = json.loads(APK_PIN.read_text()) if APK_PIN.exists() else {}
    data.update({"apk": str(path), "sha256": digest})
    APK_PIN.parent.mkdir(parents=True, exist_ok=True)
    APK_PIN.write_text(json.dumps(data, indent=2) + "\n")
    log(f"pinned {path.name} sha256={digest[:12]}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_doctor(cfg, backend, args) -> int:
    problems = []
    warnings = []

    for tool in ("adb", "emulator", "avdmanager"):
        if not shutil.which(tool):
            problems.append(f"{tool} not on PATH")
    if not shutil.which("tesseract"):
        warnings.append("tesseract missing - the OCR captcha fallback is off")

    try:
        book = sel_mod.load(cfg.paths.selectors)
        for p in sel_mod.validate(book):
            problems.append(f"selectors.yaml: {p}")
        log(f"selectors: {len(book.elements)} elements, {len(book.states)} states, "
            f"v{book.meta.get('version', '?')}")
    except Exception as exc:
        problems.append(f"selectors.yaml unreadable: {exc}")

    have = {i.idx for i in backend.list_instances()}
    for idx in cfg.run.instances:
        if idx not in have:
            problems.append(f"AVD {cfg.avd_name(idx)} does not exist - run setup")
            continue
        snap = (backend.avd_home / f"{cfg.avd_name(idx)}.avd" / "snapshots"
                / cfg.emulator.snapshot)
        if not snap.exists():
            problems.append(f"AVD {cfg.avd_name(idx)} has no "
                            f"'{cfg.emulator.snapshot}' snapshot")
        elif backend.is_booted(idx):
            pkgs = backend.adb_run(idx, "shell", "pm", "list", "packages",
                                   cfg.app.package, check=False)
            if cfg.app.package not in pkgs:
                problems.append(f"{cfg.avd_name(idx)} is booted but "
                                f"{cfg.app.package} is not installed")
            else:
                log(f"{cfg.avd_name(idx)}: {cfg.app.package} present, booted")
                
    if cfg.captcha.mode == "human" and not shutil.which("scrcpy"):
        warnings.append("scrcpy missing - no window to solve the captcha in "
                        "(brew install scrcpy), or pass --windowed")

    # ---- APK pin ---------------------------------------------------------
    if cfg.app.apk:
        apk = Path(cfg.app.apk).expanduser()
        if not apk.exists():
            problems.append(f"app.apk not found: {apk}")
        elif APK_PIN.exists():
            pinned = json.loads(APK_PIN.read_text()).get("sha256", "")
            live = hashlib.sha256(apk.read_bytes()).hexdigest()
            if pinned and pinned != live:
                problems.append(f"app.apk differs from configs/apk.json "
                                f"({live[:12]} != {pinned[:12]})")
    else:
        warnings.append("app.apk unset - the APK checks are all skipped")

    # ---- Super-Properties version ---------------------------------------
    if cfg.app.version in ("", "0.0.0"):
        problems.append("[app] version is unset - X-Super-Properties will "
                        "impersonate 0.0.0 and Discord may reject it")
    if cfg.app.build_number == 0:
        warnings.append("[app] build_number is 0")

    if cfg.net.mitm and not (Path.home() / ".mitmproxy"
                             / "mitmproxy-ca-cert.pem").exists():
        problems.append("mitm enabled but ~/.mitmproxy/mitmproxy-ca-cert.pem missing")
    if not cfg.net.no_proxy and not Path(cfg.net.proxy_file).exists():
        problems.append(f"proxy_file not found: {cfg.net.proxy_file} "
                        "(or set net.no_proxy: true)")

    for p in problems:
        log(f"  !! {p}", "err")
    for w in warnings:
        log(f"  ~  {w}", "warn")
    if problems:
        log(f"doctor: {len(problems)} problem(s), {len(warnings)} warning(s)", "err")
        return 1
    log(f"doctor: OK ({len(warnings)} warning(s))", "ok")
    return 0


def cmd_setup(cfg, backend, args) -> int:
    cfg.paths.ensure()
    if args.apk:
        cfg.app.apk = args.apk
    if cfg.app.apk:
        _pin_apk(Path(cfg.app.apk).expanduser())

    for idx in cfg.run.instances:
        name = cfg.avd_name(idx)
        if args.force or idx not in {i.idx for i in backend.list_instances()}:
            backend.create(name)

    for idx in cfg.run.instances:
        backend.start(idx)
        backend.wait_booted(idx)
        if cfg.app.apk:
            log(f"installing {cfg.app.apk} on {cfg.avd_name(idx)}")
            backend.install_apk(idx, Path(cfg.app.apk).expanduser())
        if cfg.net.mitm:
            from tools import ca_install
            ca_install.install(backend, idx)
        backend.snapshot(idx, cfg.emulator.snapshot)

    backend.stop_all()
    log(f"setup complete - snapshot '{cfg.emulator.snapshot}' saved", "ok")
    return 0


def cmd_recon(cfg, backend, args) -> int:
    from core.context import Ctx
    from core.human import HumanManager

    cfg.paths.ensure()
    store = Store(cfg.paths)
    human = HumanManager(cfg, backend)
    human.start()
    ctx = Ctx(cfg=cfg, backend=backend, store=store, human=human,
              book=sel_mod.load(cfg.paths.selectors))
    try:
        for idx in cfg.run.instances:
            dev = ctx.device(idx)
            dev.start_app()
            for name in ("01_identify", "02_after_register"):
                root = dev.dump(cached=False)
                out = cfg.paths.shots / f"{name}_inst{idx}"
                out.mkdir(parents=True, exist_ok=True)
                (out / "hierarchy.xml").write_text(
                    ET.tostring(root, encoding="unicode"))
                (out / "screen.png").write_bytes(dev.screenshot_png())
                log(f"inst-{idx} state={ctx.ui(idx).state()} -> {out}")
                if name == "01_identify" and ctx.ui(idx).tap_if("register_button"):
                    ctx.ui(idx).wait_state(["identify"], 20)
        return 0
    finally:
        human.stop()
        if not cfg.run.keep_open:
            backend.stop_all()


def cmd_run(cfg, backend, args) -> int:
    store = Store(cfg.paths)
    _plan(cfg)
    if cfg.run.dry_run:
        log("dry run - nothing written", "ok")
        return 0
    cake = _caffeinate(cfg.run.caffeinate)
    try:
        return orchestrator.run_batches(cfg, backend, store)
    finally:
        if cake:
            cake.terminate()


def cmd_finalize(cfg, backend, args) -> int:
    return orchestrator.finalize_pending(cfg, backend, Store(cfg.paths))


def cmd_resume(cfg, backend, args) -> int:
    return orchestrator.resume(cfg, backend, Store(cfg.paths))


COMMANDS = {
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "recon": cmd_recon,
    "run": cmd_run,
    "finalize": cmd_finalize,
    "resume": cmd_resume,
}
LOCKED = ("run", "finalize", "resume")


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dcgen.py")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--instances", help="comma list, e.g. 0,1")
    ap.add_argument("--count", "-n", type=int)
    ap.add_argument("--batch", type=int)
    ap.add_argument("--captcha", choices=["human", "auto", "skip"])
    ap.add_argument("--apk")
    ap.add_argument("--mitm", action="store_true")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--windowed", action="store_true",
                    help="show the AVD window instead of headless")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    overrides = {}
    if args.instances:
        overrides["run.instances"] = [int(x) for x in args.instances.split(",")]
    if args.count:
        overrides["run.count"] = args.count
    if args.batch is not None:
        overrides["run.batch"] = args.batch
    if args.captcha:
        overrides["captcha.mode"] = args.captcha
    if args.apk:
        overrides["app.apk"] = args.apk
    if args.mitm:
        overrides["net.mitm"] = True
    if args.no_proxy:
        overrides["net.no_proxy"] = True
    if args.dry_run:
        overrides["run.dry_run"] = True

    try:
        cfg = load(args.config, overrides)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2

    banner("dc-gen-android  ::  v2")
    log_file = start_file(cfg.paths.logs / "dcgen.log")

    try:
        try:
            backend = build_backend(cfg, windowed=args.windowed)
        except DCGenError as exc:
            log(f"{exc}", "err")
            return 3

        if args.command in LOCKED:
            cfg.paths.ensure()
            lock = RunLock(cfg.paths.run_lock)
            lock.acquire(force=args.force)
            try:
                return COMMANDS[args.command](cfg, backend, args)
            finally:
                lock.release()
        return COMMANDS[args.command](cfg, backend, args)
    finally:
        stop_file()


if __name__ == "__main__":
    sys.exit(main())