"""config.yaml loader and validator.

All customisation lives in config.yaml. Unknown keys are an error, so a typo
never silently does nothing. load() auto-discovers the file, so callers passing
a stale path (e.g. "config.toml") still resolve correctly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

DEFAULT_CONFIG = "config.yaml"
CANDIDATES = ("config.yaml", "config.yml", "config/config.yaml",
              "configs/config.yaml", "config.toml")
VALID_CAPTCHA = ("human", "auto", "skip")
VALID_EMAIL_SOURCES = ("gmail-plus", "file")
VALID_NAME_STYLES = ("adjective_noun", "email_local", "list")
VALID_USERNAME_STYLES = ("word_digits", "email_local", "digits")
VALID_FORMATS = ("csv", "json", "both")


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
@dataclass
class RunCfg:
    accounts: int = 5
    emulators: int = 1
    max_consecutive_failures: int = 3
    cooldown_s: float = 20.0
    avd_base_name: str = "dcgen"
    dry_run: bool = False


@dataclass
class EmulatorCfg:
    auto_boot: bool = True
    headless: bool = False
    no_snapshot_load: bool = True
    ram_mb: int = 2560
    base_port: int = 5554
    boot_timeout_s: int = 300
    extra_args: list = field(default_factory=list)


@dataclass
class AccountsCfg:
    email_source: str = "gmail-plus"
    email_base: str = "you@gmail.com"
    tag_pattern: str = "dcgen{n}"
    email_file: str = "emails.txt"
    password: str = "S3cretPassw0rd!"
    start_index: int = 1


@dataclass
class IdentityCfg:
    name_style: str = "adjective_noun"
    display_names: list = field(default_factory=list)
    name_separator: str = ""
    name_max_len: int = 20
    name_adjectives: list = field(default_factory=list)
    name_nouns: list = field(default_factory=list)

    username_style: str = "word_digits"
    username_prefix: str = ""
    username_digits: int = 6
    username_separator: str = ""
    username_max_len: int = 20
    username_words: list = field(default_factory=list)

    used_names_file: str = "state/used_names.txt"
    used_usernames_file: str = "state/used_usernames.txt"


@dataclass
class DobCfg:
    year_min: int = 1995
    year_max: int = 2005
    day_max: int = 28
    pin_year: int = 0
    pin_month: int = 0
    pin_day: int = 0


@dataclass
class CaptchaCfg:
    mode: str = "human"
    human_timeout_s: int = 600
    beep: bool = True
    auto_rounds: int = 10


@dataclass
class FlowCfg:
    retries: int = 2
    submit_timeout_s: int = 45
    captcha_wait_s: int = 25
    fresh_per_account: bool = True


@dataclass
class ProxyCfg:
    enabled: bool = False
    file: str = "proxies.txt"


@dataclass
class OutputCfg:
    dir: str = "accounts"
    format: str = "both"
    credentials_file: str = "state/credentials.txt"


@dataclass
class ArtifactsCfg:
    dir: str = "artifacts"
    snap_every_step: bool = False
    keep_success_artifacts: bool = False


@dataclass
class LoggingCfg:
    level: str = "info"
    prefix_serial: bool = True


@dataclass
class Config:
    run: RunCfg = field(default_factory=RunCfg)
    emulator: EmulatorCfg = field(default_factory=EmulatorCfg)
    accounts: AccountsCfg = field(default_factory=AccountsCfg)
    identity: IdentityCfg = field(default_factory=IdentityCfg)
    dob: DobCfg = field(default_factory=DobCfg)
    captcha: CaptchaCfg = field(default_factory=CaptchaCfg)
    flow: FlowCfg = field(default_factory=FlowCfg)
    proxy: ProxyCfg = field(default_factory=ProxyCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    artifacts: ArtifactsCfg = field(default_factory=ArtifactsCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    path: Path = Path(DEFAULT_CONFIG)

    # ---- derived ---------------------------------------------------------
    @property
    def serials(self):
        from core.ui import serial_for
        return [serial_for(i, self.emulator.base_port)
                for i in range(self.run.emulators)]

    def avd_name(self, instance: int) -> str:
        return f"{self.run.avd_base_name}-{instance}"

    def slots(self):
        """Account slots, round-robin across emulators: [(index, instance)]."""
        start = self.accounts.start_index
        return [(start + k, k % self.run.emulators) for k in range(self.run.accounts)]

    def as_dict(self):
        d = asdict(self)
        d.pop("path", None)
        return d


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
SECTIONS = ("run", "emulator", "accounts", "identity", "dob", "captcha",
            "flow", "proxy", "output", "artifacts", "logging")


def _resolve(path) -> Path:
    """Explicit path if it exists, otherwise the first candidate that does.

    A stale explicit path (e.g. "config.toml" from an older CLI default) must
    NOT hard-fail - it should fall through to discovery.
    """
    requested = str(path) if path else None
    if path:
        p = Path(path)
        if p.exists():
            return p
    for cand in CANDIDATES:
        if Path(cand).exists():
            return Path(cand)
    looked = ", ".join(([requested] if requested else []) + list(CANDIDATES))
    raise ConfigError(f"no config found. Looked for: {looked}\n"
                      "  create one with: ./run.sh -m tools.setup")


def _fill(obj, data, section):
    if data is None:
        return
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a mapping, got {type(data).__name__}")
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ConfigError(f"unknown key: [{section}] {key}")
        setattr(obj, key, value)


def _yaml_hint(path, exc) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    ln, col = mark.line + 1, mark.column
    if not (1 <= ln <= len(lines)):
        return ""
    src = lines[ln - 1]
    pad = " " * len(str(ln))
    return (f"  {ln} | {src}\n"
            f"  {pad} | " + " " * max(0, col) + "^\n"
            "  usual causes: a tab where spaces belong, or an unquoted value "
            "containing ':' or '{'")


def load(path=None, overrides: dict | None = None) -> Config:
    p = _resolve(path)
    with open(p, "rb") as fh:
        try:
            raw = yaml.safe_load(fh) or {}
        except yaml.MarkedYAMLError as exc:
            raise ConfigError(f"{p}: {exc.problem}\n{_yaml_hint(p, exc)}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping")

    cfg = Config(path=p)
    for section in SECTIONS:
        _fill(getattr(cfg, section), raw.get(section), section)

    for key, value in (overrides or {}).items():
        section, _, name = key.partition(".")
        target = getattr(cfg, section, None)
        if target is None:
            raise ConfigError(f"unknown override section: {section}")
        if not hasattr(target, name):
            raise ConfigError(f"unknown override: {key}")
        setattr(target, name, value)

    validate(cfg)
    return cfg


def validate(cfg: Config):
    if cfg.run.emulators < 1:
        raise ConfigError("[run] emulators must be >= 1")
    if cfg.run.accounts < 1:
        raise ConfigError("[run] accounts must be >= 1")
    if cfg.accounts.email_source not in VALID_EMAIL_SOURCES:
        raise ConfigError(f"[accounts] email_source must be one of {VALID_EMAIL_SOURCES}")
    if cfg.accounts.email_source == "gmail-plus" and "@" not in str(cfg.accounts.email_base):
        raise ConfigError("[accounts] email_base must look like an address")
    if cfg.accounts.email_source == "file" and not Path(cfg.accounts.email_file).exists():
        raise ConfigError(f"[accounts] email_file not found: {cfg.accounts.email_file}")
    if cfg.identity.name_style not in VALID_NAME_STYLES:
        raise ConfigError(f"[identity] name_style must be one of {VALID_NAME_STYLES}")
    if cfg.identity.username_style not in VALID_USERNAME_STYLES:
        raise ConfigError(f"[identity] username_style must be one of {VALID_USERNAME_STYLES}")
    if cfg.captcha.mode not in VALID_CAPTCHA:
        raise ConfigError(f"[captcha] mode must be one of {VALID_CAPTCHA}")
    if cfg.output.format not in VALID_FORMATS:
        raise ConfigError(f"[output] format must be one of {VALID_FORMATS}")
    if cfg.dob.year_min > cfg.dob.year_max:
        raise ConfigError("[dob] year_min is after year_max")
    if not (1 <= cfg.dob.day_max <= 28):
        raise ConfigError("[dob] day_max must be 1..28 (28 dodges month length)")
    if cfg.emulator.base_port < 5554:
        raise ConfigError("[emulator] base_port must be >= 5554")
    if cfg.proxy.enabled and not Path(cfg.proxy.file).exists():
        raise ConfigError(f"[proxy] file not found: {cfg.proxy.file}")


DEFAULT_YAML = '''# dc-gen-android :: configuration

run:
  accounts: 5
  emulators: 1

accounts:
  email_source: "gmail-plus"
  email_base: "you@gmail.com"

captcha:
  mode: "human"

identity:
  name_style: "adjective_noun"
  username_style: "word_digits"
'''


def write_default(path=DEFAULT_CONFIG, force=False) -> Path:
    p = Path(path)
    if p.exists() and not force:
        return p
    p.write_text(DEFAULT_YAML, encoding="utf-8")
    return p