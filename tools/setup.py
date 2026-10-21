"""Interactive setup for config.yaml. No editor required.

    ./run.sh -m tools.setup            # ask, then write config.yaml
    ./run.sh -m tools.setup --show     # print the current settings
    ./run.sh -m tools.setup --quick    # only accounts / emulators / email
    ./run.sh -m tools.setup --edit     # write, then open it in the GUI editor
    ./run.sh -m tools.setup --defaults # accept everything, no prompts

Choice prompts are numbered. Type the number, or the name. Press Enter to keep
the value in brackets, ? for help, q to keep the default and move on. Ctrl-C
aborts without writing anything.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from shutil import which

from core.config import CANDIDATES, Config, ConfigError, load, validate

CFG = Path("config.yaml")

STEP = {"n": 0, "total": 0}


def header(text):
    print(f"\n-- {text} --")


def numbered(choices):
    print("    " + "    ".join(f"{i + 1}) {c}" for i, c in enumerate(choices)))


def match_choice(raw, choices):
    low = raw.lower()
    for c in choices:
        if str(c).lower() == low:
            return c
    if raw.isdigit():
        i = int(raw) - 1
        if 0 <= i < len(choices):
            return choices[i]
    starts = [c for c in choices if str(c).lower().startswith(low)]
    return starts[0] if len(starts) == 1 else None


def ask(label, current, cast=str, choices=None, note=""):
    while True:
        STEP["n"] += 1
        if note:
            print(f"    {note}")
        if choices:
            numbered(choices)
        hint = f" [{current}]" if current not in (None, "") else ""
        try:
            raw = input(f"  ({STEP['n']}/{STEP['total']}) {label}{hint}: ").strip()
        except EOFError:
            return current

        if raw == "" or raw.lower() == "q":
            return current
        if raw == "?":
            STEP["n"] -= 1
            continue

        if choices:
            value = match_choice(raw, choices)
            if value is None:
                print(f"    '{raw}' is not an option - pick a number, "
                      f"or q to keep the default")
                STEP["n"] -= 1
                continue
            return value

        try:
            return cast(raw)
        except (TypeError, ValueError):
            print("    not valid, try again")
            STEP["n"] -= 1


def ask_bool(label, current):
    STEP["n"] += 1
    while True:
        try:
            raw = input(f"  ({STEP['n']}) {label} "
                        f"[{'Y/n' if current else 'y/N'}]: ").strip().lower()
        except EOFError:
            return current
        if raw == "":
            return current
        if raw in ("y", "yes", "true", "t", "1"):
            return True
        if raw in ("n", "no", "false", "f", "0"):
            return False
        print("    answer y or n")


def ask_email(label, current):
    STEP["n"] += 1
    while True:
        try:
            raw = input(f"  ({STEP['n']}) {label} [{current}]: ").strip()
        except EOFError:
            return current
        raw = raw or current
        if "@" in raw and "." in raw.split("@")[-1]:
            return raw
        print("    that does not look like an address")


def ask_tag(label, current):
    """A pattern with {n}, or a bare number, or a prefix. Anything else is a bug."""
    STEP["n"] += 1
    while True:
        print('    "{n}" becomes the account number, e.g. dcgen{n} -> +dcgen1')
        print('    or type a bare number to start there, e.g. 1 -> +1, +2, +3')
        try:
            raw = input(f"  ({STEP['n']}) {label} [{current}]: ").strip()
        except EOFError:
            return current
        if raw == "" or raw.lower() == "q":
            return current
        if "{n" in raw:
            return raw
        if raw.isdigit():
            n = int(raw)
            print(f"    no {{n}} - treating it as the starting number: "
                  f"+{n}, +{n + 1}, +{n + 2}, ...")
            return raw
        if raw.endswith("+"):
            print(f"    no {{n}} - treating it as a prefix: +{raw}1, +{raw}2, ...")
            return raw
        print(f"    no {{n}} - treating it as a prefix: +{raw}1, +{raw}2, ...")
        return raw


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def flow_list(items):
    return "[]" if not items else "[" + ", ".join(f'"{i}"' for i in items) + "]"


def render(cfg: Config) -> str:
    return f'''# =============================================================================
# dc-gen-android :: configuration
# Written by tools/setup.py - safe to hand-edit.
# =============================================================================

run:
  # Total accounts to create across ALL emulators.
  accounts: {cfg.run.accounts}
  # Emulators in parallel. Instance i uses port 5554 + 2*i
  # and AVD "<avd_base_name>-<i>", i.e. dcgen-0, dcgen-1, ...
  emulators: {cfg.run.emulators}
  # Give up on an emulator after this many failures in a row.
  max_consecutive_failures: {cfg.run.max_consecutive_failures}
  # Pause between accounts on the same emulator, in seconds.
  cooldown_s: {cfg.run.cooldown_s}
  avd_base_name: "{cfg.run.avd_base_name}"
  dry_run: {str(cfg.run.dry_run).lower()}

emulator:
  auto_boot: {str(cfg.emulator.auto_boot).lower()}
  # Hide the window. Keep it visible to solve the captcha.
  headless: {str(cfg.emulator.headless).lower()}
  no_snapshot_load: {str(cfg.emulator.no_snapshot_load).lower()}
  ram_mb: {cfg.emulator.ram_mb}
  base_port: {cfg.emulator.base_port}
  boot_timeout_s: {cfg.emulator.boot_timeout_s}
  extra_args: {flow_list(cfg.emulator.extra_args)}

accounts:
  # gmail-plus | file
  email_source: "{cfg.accounts.email_source}"
  email_base: "{cfg.accounts.email_base}"
  # "{{n}}" becomes the account number. A bare number means start there.
  # Quoted, or YAML reads the braces as a mapping.
  tag_pattern: "{cfg.accounts.tag_pattern}"
  email_file: "{cfg.accounts.email_file}"
  password: "{cfg.accounts.password}"
  start_index: {cfg.accounts.start_index}

identity:
  # adjective_noun | email_local | list
  name_style: "{cfg.identity.name_style}"
  display_names: {flow_list(cfg.identity.display_names)}
  name_separator: "{cfg.identity.name_separator}"
  name_max_len: {cfg.identity.name_max_len}
  name_adjectives: {flow_list(cfg.identity.name_adjectives)}
  name_nouns: {flow_list(cfg.identity.name_nouns)}

  # word_digits | email_local | digits
  username_style: "{cfg.identity.username_style}"
  username_prefix: "{cfg.identity.username_prefix}"
  username_digits: {cfg.identity.username_digits}
  username_separator: "{cfg.identity.username_separator}"
  username_max_len: {cfg.identity.username_max_len}
  username_words: {flow_list(cfg.identity.username_words)}

  used_names_file: "{cfg.identity.used_names_file}"
  used_usernames_file: "{cfg.identity.used_usernames_file}"

dob:
  year_min: {cfg.dob.year_min}
  year_max: {cfg.dob.year_max}
  day_max: {cfg.dob.day_max}
  pin_year: {cfg.dob.pin_year}
  pin_month: {cfg.dob.pin_month}
  pin_day: {cfg.dob.pin_day}

captcha:
  # human | auto | skip
  mode: "{cfg.captcha.mode}"
  human_timeout_s: {cfg.captcha.human_timeout_s}
  beep: {str(cfg.captcha.beep).lower()}
  auto_rounds: {cfg.captcha.auto_rounds}

flow:
  retries: {cfg.flow.retries}
  submit_timeout_s: {cfg.flow.submit_timeout_s}
  captcha_wait_s: {cfg.flow.captcha_wait_s}
  fresh_per_account: {str(cfg.flow.fresh_per_account).lower()}

proxy:
  enabled: {str(cfg.proxy.enabled).lower()}
  file: "{cfg.proxy.file}"

output:
  dir: "{cfg.output.dir}"
  # csv | json | both
  format: "{cfg.output.format}"
  credentials_file: "{cfg.output.credentials_file}"

artifacts:
  dir: "{cfg.artifacts.dir}"
  snap_every_step: {str(cfg.artifacts.snap_every_step).lower()}
  keep_success_artifacts: {str(cfg.artifacts.keep_success_artifacts).lower()}

logging:
  level: "{cfg.logging.level}"
  prefix_serial: {str(cfg.logging.prefix_serial).lower()}
'''


def show(cfg: Config):
    print(f"  accounts            {cfg.run.accounts}")
    print(f"  emulators           {cfg.run.emulators}")
    print(f"  email_source        {cfg.accounts.email_source}")
    print(f"  email_base          {cfg.accounts.email_base}")
    print(f"  tag_pattern         {cfg.accounts.tag_pattern}")
    print(f"  password            {cfg.accounts.password}")
    print(f"  name_style          {cfg.identity.name_style}")
    print(f"  username_style      {cfg.identity.username_style}")
    print(f"  dob range           {cfg.dob.year_min}-{cfg.dob.year_max}")
    print(f"  captcha mode        {cfg.captcha.mode}")
    print(f"  fresh per account   {cfg.flow.fresh_per_account}")
    print(f"  headless            {cfg.emulator.headless}")
    print(f"  output              {cfg.output.dir} ({cfg.output.format})")


def open_editor(path: Path):
    if which("code"):
        subprocess.Popen(["code", str(path)])
    elif which("open"):
        subprocess.Popen(["open", "-e", str(path)])
    else:
        print(f"  open {path} in your editor")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="print current settings")
    ap.add_argument("--quick", action="store_true", help="only the essentials")
    ap.add_argument("--defaults", action="store_true", help="accept everything")
    ap.add_argument("--edit", action="store_true", help="open the file afterwards")
    ap.add_argument("--no-dry-run", action="store_true", help="skip the offer")
    args = ap.parse_args(argv)

    existing = next((Path(c) for c in CANDIDATES if Path(c).exists()), None)
    cfg = load(existing) if existing else Config()
    cfg.path = CFG

    if args.show:
        print(f"current settings ({existing or 'defaults'}):")
        show(cfg)
        return 0

    STEP["total"] = 4 if args.quick else 18
    print("dc-gen-android :: setup")
    if existing:
        print(f"loaded {existing} - Enter keeps each current value")
    else:
        print("no config yet - Enter accepts the default in brackets")
    print("? shows the options again, q keeps the default, Ctrl-C aborts\n")

    try:
        if not args.defaults:
            if not args.quick:
                header("how much work")
            cfg.run.accounts = ask("accounts to create", cfg.run.accounts, int,
                                   note="total, across every emulator")
            cfg.run.emulators = ask("emulators in parallel", cfg.run.emulators, int,
                                    note="1 is fine - each one needs its own AVD")
            if not args.quick:
                cfg.run.cooldown_s = ask("seconds between accounts", cfg.run.cooldown_s,
                                         float)

            header("emails")
            cfg.accounts.email_source = ask(
                "email source", cfg.accounts.email_source,
                choices=["gmail-plus", "file"],
                note="pick a NUMBER: gmail-plus reuses one mailbox with +tags")
            if cfg.accounts.email_source == "gmail-plus":
                cfg.accounts.email_base = ask_email("your gmail address",
                                                    cfg.accounts.email_base)
                if not args.quick:
                    cfg.accounts.tag_pattern = ask_tag("tag pattern",
                                                       cfg.accounts.tag_pattern)
            else:
                cfg.accounts.email_file = ask("emails file", cfg.accounts.email_file)

            if not args.quick:
                cfg.accounts.password = ask("password", cfg.accounts.password)

                header("identity")
                cfg.identity.name_style = ask(
                    "display name style", cfg.identity.name_style,
                    choices=["adjective_noun", "email_local", "list"])
                cfg.identity.username_style = ask(
                    "username style", cfg.identity.username_style,
                    choices=["word_digits", "email_local", "digits"])
                if cfg.identity.username_style == "word_digits":
                    cfg.identity.username_digits = ask("digits after the word",
                                                       cfg.identity.username_digits, int)

                header("date of birth")
                cfg.dob.year_min = ask("youngest year", cfg.dob.year_min, int)
                cfg.dob.year_max = ask("oldest year", cfg.dob.year_max, int)

                header("captcha")
                cfg.captcha.mode = ask(
                    "captcha mode", cfg.captcha.mode,
                    choices=["human", "auto", "skip"],
                    note="human = you solve it in the AVD window")
                cfg.captcha.beep = ask_bool("ring the bell when it appears",
                                            cfg.captcha.beep)
                cfg.captcha.human_timeout_s = ask("wait seconds for you",
                                                  cfg.captcha.human_timeout_s, int)

                header("run style")
                cfg.flow.fresh_per_account = ask_bool("wipe Discord between accounts",
                                                      cfg.flow.fresh_per_account)
                cfg.emulator.headless = ask_bool("hide the emulator window",
                                                 cfg.emulator.headless)
                cfg.artifacts.snap_every_step = ask_bool("save a screenshot every step",
                                                         cfg.artifacts.snap_every_step)
    except KeyboardInterrupt:
        print("\n\naborted - nothing written")
        return 130

    try:
        validate(cfg)
    except ConfigError as exc:
        print(f"\ninvalid: {exc}")
        return 2

    CFG.write_text(render(cfg), encoding="utf-8")
    print(f"\nwrote {CFG}\n")
    print("summary:")
    show(cfg)

    if cfg.emulator.headless and cfg.captcha.mode == "human":
        print("\n  heads-up: headless is on but the captcha is human, so there")
        print("  is no window to click in. Set 'hide the emulator window' to n,")
        print("  or attach scrcpy and click there instead.")

    if args.edit:
        open_editor(CFG)

    print("\nnext:")
    print("  ./run.sh main.py --dry-run     # see the plan")
    print("  ./run.sh main.py               # go")

    if not args.no_dry_run:
        try:
            if input("\nrun the dry run now? [Y/n]: ").strip().lower() not in ("n", "no"):
                return subprocess.call([sys.executable, "main.py", "--dry-run"])
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())