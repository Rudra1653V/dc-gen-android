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
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import orchestrator
from core.config import ConfigError, load
from core.errors import DCGenError
from core.log import banner, log, start_file, stop_file
from core.state import RunLock, Store
from device import selectors as sel_mod

DEFAULT_CONFIG = "configs/config.yaml"


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


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_doctor(cfg, backend, args) -> int:
    problems = []

    for tool in ("adb", "emulator", "avdmanager"):
        if not shutil.which(tool):
            problems.append(f"{tool} not on PATH")
    if not shutil.which("tesseract"):
        problems.append("tesseract missing - OCR captcha fallback is off")

    try:
        book = sel_mod.load(cfg.paths.selectors)
        for p in sel_mod.validate(book):
            problems.append(f"selectors.yaml: {p}")
        log(f"selectors: {len(book.elements)} elements, "
            f"{len(book.states)} states, v{book.meta.get('version', '?')}")
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

    if cfg.app.apk and not Path(cfg.app.apk).exists():
        problems.append(f"app.apk not found: {cfg.app.apk}")
    if cfg.net.mitm and not (Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem").exists():
        problems.append("mitm enabled but ~/.mitmproxy/mitmproxy-ca-cert.pem missing")
    if not cfg.net.no_proxy and not Path(cfg.net.proxy_file).exists():
        problems.append(f"proxy_file not found: {cfg.net.proxy_file} "
                        "(or set net.no_proxy: true)")

    for p in problems:
        log(f"  !! {p}", "err")
    log("doctor: OK" if not problems else f"doctor: {len(problems)} problem(s)",
        "ok" if not problems else "err")
    return 1 if problems else 0


def cmd_setup(cfg, backend, args) -> int:
    cfg.paths.ensure()
    if args.apk:
        cfg.app.apk = args.apk
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
                    __import__("xml.etree.ElementTree", fromlist=["tostring"])
                    .tostring(root, encoding="unicode"))
                (out / "screen.png").write_bytes(dev.screenshot_png())
                log(f"inst-{idx} state={ctx.ui(idx).state()} -> {out}")
                if name == "01_identify":
                    if ctx.ui(idx).tap_if("register_button"):
                        ctx.ui(idx).wait_state(["identify"], 20)
        return 0
    finally:
        human.stop()
        if not cfg.run.keep_open:
            backend.stop_all()


def _with_lock(cfg, fn) -> int:
    cfg.paths.ensure()
    lock = RunLock(cfg.paths.run_lock)
    lock.acquire(force=getattr(fn, "force", False))
    try:
        return fn()
    finally:
        lock.release()


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
    store = Store(cfg.paths)
    return orchestrator.finalize_pending(cfg, backend, store)


def cmd_resume(cfg, backend, args) -> int:
    store = Store(cfg.paths)
    return orchestrator.resume(cfg, backend, store)


COMMANDS = {
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "recon": cmd_recon,
    "run": cmd_run,
    "finalize": cmd_finalize,
    "resume": cmd_resume,
}


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
        backend = build_backend(cfg, windowed=args.windowed)
    except DCGenError as exc:
        log(f"{exc}", "err")
        return 3

    try:
        if args.command in ("run", "finalize", "resume"):
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
        log(f"log -> {log_file}")


if __name__ == "__main__":
    sys.exit(main())