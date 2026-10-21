"""Single-instance recon driver, config-aware.

    python -m tools.walk 0 --email you+dcgen1@gmail.com
    python -m tools.walk 0 --email ... --captcha-mode human
"""
from __future__ import annotations

import argparse
import sys
import traceback

from core.accounts import AccountFactory
from core.config import ConfigError, load
from core.ui import Device, UiError, load_selectors, serial_for
from flows.register import Signup


def snap(dev, sel, name, note=None):
    dev.ensure_ready()
    nodes = dev.snapshot(name)
    print(f"\n== {name}  focus={dev.current_focus()}")
    for n in nodes:
        if n.cls.endswith("EditText") or n.text or n.desc:
            print(f"  {n}")
    print(f"   state={dev.state(sel)}")
    if note:
        print(f"   {note}")
    return nodes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("instance", type=int)
    ap.add_argument("--email", help="override the generated address")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--captcha-mode", choices=["human", "auto", "skip"], default=None)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2
    sel = load_selectors()

    factory = AccountFactory(cfg, seed=args.seed)
    acct = factory.make(cfg.accounts.start_index, args.instance)
    if args.email:
        acct.email = args.email

    dev = Device(serial_for(args.instance, cfg.emulator.base_port))
    dev.ensure_online(boot_timeout=cfg.emulator.boot_timeout_s)
    if args.fresh:
        dev.pm_clear()
    print(f"$ start_app -> {dev.start_app()!r}")
    print(f"$ account #{acct.index} {acct.email} name={acct.display_name!r} "
          f"username={acct.username!r} dob={acct.dob_text}")

    mode = args.captcha_mode or cfg.captcha.mode
    signup = Signup(dev, sel, acct, log=print, cfg=cfg)

    try:
        snap(dev, sel, "00_welcome")
        signup.step_welcome()
        snap(dev, sel, "01_identify")
        signup.step_identify()
        snap(dev, sel, "02_email_filled")
        signup.step_email_next()
        snap(dev, sel, "03_register_name")
        signup.step_name()
        snap(dev, sel, "04_register_form")
        signup.step_form_fill()
        snap(dev, sel, "04b_form_filled")
        signup.step_form_next()
        snap(dev, sel, "05_after_form")
        signup.step_dob_open()
        snap(dev, sel, "06_dob_picker")
        signup.step_dob_spin()
        snap(dev, sel, "07_dob_set")
        outcome = signup.step_submit()
        snap(dev, sel, "08_after_submit", f"outcome={outcome}")
        if outcome == "human_check":
            signup.step_human_check()
        snap(dev, sel, "09_after_confirm")
        state = signup.step_captcha(mode=mode)
        snap(dev, sel, "10_after_captcha", f"mode={mode} -> {state}")
        print("\ndone - paste the last block above")
        return 0
    except UiError as exc:
        print(f"\n!! failed: {exc}")
        try:
            print(f"   state={dev.state(sel)}")
            dev.write_raw(dev.raw_dump())
        except Exception:
            pass
        traceback.print_exc(limit=1)
        return 1


if __name__ == "__main__":
    sys.exit(main())