"""config.yaml loader and validator for the v2 spine.

All customisation lives in configs/config.yaml. Unknown keys are an error, so a
typo never silently does nothing. Env overrides use DCGEN_<section>__<attr>,
e.g. DCGEN_NET__MITM=1 or DCGEN_RUN__COUNT=25.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

from core.paths import Paths

CANDIDATES = ("configs/config.yaml", "config.yaml", "config.yml",
              "config/config.yaml")

VALID_CAPTCHA = ("human", "auto", "skip")
VALID_HUMAN = ("park", "block", "assist")
VALID_BACKENDS = ("avd",)
VALID_PROXY_MODES = ("rotate", "sticky", "fixed")
VALID_EMAIL_SOURCES = ("catchall", "gmail-plus", "imap", "tempmail-api", "manual")
VALID_NAME_STYLES = ("adjective_noun", "email_local", "list")
VALID_USERNAME_STYLES = ("word_digits", "email_local", "digits")


class ConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
@dataclass
class EmulatorCfg:
    prefix: str = "dcgen-"
    mem_mb: int = 1536
    cores: int = 2
    data_partition: str = "2048M"
    heap_mb: int = 256
    snapshot: str = "clean"
    gpu_headless: str = "swiftshader_indirect"
    gpu_windowed: str = "host"
    writable_system: bool = True
    console_port_base: int = 5554
    adb_port_base: int = 5555
    proxy_port_base: int = 8800
    boot_timeout: int = 420
    device_profile: str = "pixel_7"
    image: str = "system-images;android-34;google_apis;arm64-v8a"


@dataclass
class DeviceCfg:
    fix_resolution: bool = True
    width: int = 1080
    height: int = 1920
    dpi: int = 320
    locale: str = "en-US"
    timezone: str = "America/New_York"
    match_proxy_timezone: bool = True
    disable_animations: bool = True


@dataclass
class NetCfg:
    proxy_file: str = "proxies.txt"
    proxy_mode: str = "rotate"
    no_proxy: bool = False
    mitm: bool = False
    handshake_timeout: float = 10.0
    dead_cooldown: float = 60.0


@dataclass
class EmailCfg:
    source: str = "catchall"
    domain: str = ""
    base: str = ""
    inbox_open: str = "auto"
    inbox_search: str = "https://mail.google.com/mail/u/0/#search/to%3A{addr}"
    verify_mode: str = "paste"
    verify_timeout: int = 1800


@dataclass
class AccountCfg:
    password: str = "S3cretPassw0rd!"
    #: {n} becomes the account number, so "dcgen{n}" -> dcgen1@domain
    tag_pattern: str = "dcgen{n}"
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

    used_names_file: str = "outputs/state/used_names.txt"
    used_usernames_file: str = "outputs/state/used_usernames.txt"


@dataclass
class DobCfg:
    year_min: int = 1995
    year_max: int = 2005
    day_max: int = 28
    pin_year: int = 0
    pin_month: int = 0
    pin_day: int = 0


@dataclass
class HumanCfg:
    mode: str = "park"
    auto_captcha: bool = True
    captcha_timeout: int = 900
    webhook: str = ""


@dataclass
class CaptchaCfg:
    mode: str = "human"
    human_timeout_s: int = 600
    beep: bool = True
    auto_rounds: int = 10


@dataclass
class SmsCfg:
    enabled: bool = False
    provider: str = ""
    key: str = ""
    country: str = ""
    max_price: float = 0.0
    retries: int = 2
    release_phone: bool = False


@dataclass
class AppCfg:
    apk: str | None = None
    version: str = "0.0.0"
    build_number: int = 0
    package: str = "com.discord"
    activity: str = "com.discord/.main.MainActivity"
    browser: str = "com.android.chrome"


@dataclass
class FlowCfg:
    retries: int = 2
    submit_timeout_s: int = 45
    captcha_wait_s: int = 25
    fresh_per_account: bool = True


@dataclass
class RunCfg:
    count: int = 1
    batch: int = 0
    concurrency: int = 0
    instances: list = field(default_factory=lambda: [0])
    backend: str = "avd"
    dry_run: bool = False
    report: bool = False
    capture_on_fail: bool = True
    keep_open: bool = False
    fresh_app: bool = False
    rotate_identity: bool = False
    infinite: bool = False
    from_step: str = "auto"
    min_submit_gap: float = 20.0
    caffeinate: bool = True
    dashboard: str = "plain"


@dataclass
class OutputCfg:
    dir: str = "outputs"
    format: str = "both"


@dataclass
class Config:
    emulator: EmulatorCfg = field(default_factory=EmulatorCfg)
    device: DeviceCfg = field(default_factory=DeviceCfg)
    net: NetCfg = field(default_factory=NetCfg)
    email: EmailCfg = field(default_factory=EmailCfg)
    account: AccountCfg = field(default_factory=AccountCfg)
    identity: IdentityCfg = field(default_factory=IdentityCfg)
    dob: DobCfg = field(default_factory=DobCfg)
    human: HumanCfg = field(default_factory=HumanCfg)
    captcha: CaptchaCfg = field(default_factory=CaptchaCfg)
    sms: SmsCfg = field(default_factory=SmsCfg)
    app: AppCfg = field(default_factory=AppCfg)
    flow: FlowCfg = field(default_factory=FlowCfg)
    run: RunCfg = field(default_factory=RunCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    paths: Paths = field(default_factory=Paths)
    source: Path | None = None

    # ---- derived ---------------------------------------------------------
    @property
    def concurrency(self) -> int:
        """Workers to run in parallel. 0 means 'one per instance'."""
        return self.run.concurrency or max(1, len(self.run.instances))

    def instance_ports(self, idx: int) -> tuple[int, int, int]:
        e = self.emulator
        return (e.console_port_base + 2 * idx, e.adb_port_base + 2 * idx,
                e.proxy_port_base + 2 * idx)

    def avd_name(self, idx: int) -> str:
        return f"{self.emulator.prefix}{idx}"

    def slots(self) -> list[tuple[int, int]]:
        """(account_index, instance) round-robin across run.instances."""
        insts = self.run.instances or [0]
        n = len(insts)
        return [(self.account.start_index + k, insts[k % n])
                for k in range(self.run.count)]

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("paths", None)
        d.pop("source", None)
        return d


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
SECTIONS = ("emulator", "device", "net", "email", "account", "identity", "dob",
            "human", "captcha", "sms", "app", "flow", "run", "output")


def _resolve(path) -> Path:
    if path:
        p = Path(path)
        if p.exists():
            return p
    for cand in CANDIDATES:
        if Path(cand).exists():
            return Path(cand)
    looked = ", ".join(([str(path)] if path else []) + list(CANDIDATES))
    raise ConfigError(f"no config found. Looked for: {looked}\n"
                      "  create one with: ./run.sh -m tools.setup")


def _fill(obj, data, section) -> None:
    if data is None:
        return
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a mapping, got {type(data).__name__}")
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ConfigError(f"unknown key: [{section}] {key}")
        setattr(obj, key, value)


def _env_overrides() -> dict:
    """DCGEN_NET__MITM=1 -> {'net.mitm': '1'} (coerced in _apply_override)."""
    out: dict[str, str] = {}
    for raw, value in os.environ.items():
        if not raw.startswith("DCGEN_"):
            continue
        key = raw[len("DCGEN_"):].lower()
        if "__" not in key:
            continue
        section, _, name = key.partition("__")
        out[f"{section}.{name}"] = value
    return out


def _coerce(current, value):
    if isinstance(current, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        if isinstance(value, list):
            return value
        return [v.strip() for v in str(value).split(",") if v.strip()]
    return value


def _apply_override(cfg: Config, key: str, value) -> None:
    section, _, name = key.partition(".")
    target = getattr(cfg, section, None)
    if target is None:
        raise ConfigError(f"unknown override section: {section}")
    if not hasattr(target, name):
        raise ConfigError(f"unknown override: {key}")
    setattr(target, name, _coerce(getattr(target, name), value))


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

    cfg = Config(source=p)
    for section in SECTIONS:
        _fill(getattr(cfg, section), raw.get(section), section)

    merged = {**_env_overrides(), **(overrides or {})}
    for key, value in merged.items():
        _apply_override(cfg, key, value)

    root = p.parent.parent if p.parent.name == "configs" else p.parent
    cfg.paths = Paths(root=root, out_dir=root / cfg.output.dir)

    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not cfg.run.instances:
        raise ConfigError("[run] instances must not be empty")
    if any(i < 0 for i in cfg.run.instances):
        raise ConfigError("[run] instances must be >= 0")
    if cfg.run.count < 1 and not cfg.run.infinite:
        raise ConfigError("[run] count must be >= 1 (or set infinite: true)")
    if cfg.run.backend not in VALID_BACKENDS:
        raise ConfigError(f"[run] backend must be one of {VALID_BACKENDS}")
    if cfg.emulator.console_port_base < 5554:
        raise ConfigError("[emulator] console_port_base must be >= 5554")
    if cfg.emulator.proxy_port_base < 1024:
        raise ConfigError("[emulator] proxy_port_base must be >= 1024")
    if cfg.net.proxy_mode not in VALID_PROXY_MODES:
        raise ConfigError(f"[net] proxy_mode must be one of {VALID_PROXY_MODES}")
    if not cfg.net.no_proxy and cfg.net.proxy_file:
        if not Path(cfg.net.proxy_file).exists() and not cfg.net.mitm:
            raise ConfigError(f"[net] proxy_file not found: {cfg.net.proxy_file}")
    if cfg.email.source not in VALID_EMAIL_SOURCES:
        raise ConfigError(f"[email] source must be one of {VALID_EMAIL_SOURCES}")
    if cfg.email.source == "catchall" and not cfg.email.domain:
        raise ConfigError("[email] source 'catchall' needs [email] domain")
    if cfg.email.source == "gmail-plus" and "@" not in cfg.email.base:
        raise ConfigError("[email] 'gmail-plus' needs [email] base = you@gmail.com")
    if cfg.email.verify_mode not in ("paste", "api"):
        raise ConfigError("[email] verify_mode must be 'paste' or 'api'")
    if not cfg.account.password:
        raise ConfigError("[account] password must not be empty")
    if not cfg.account.tag_pattern:
        raise ConfigError("[account] tag_pattern must not be empty")
    if cfg.identity.name_style not in VALID_NAME_STYLES:
        raise ConfigError(f"[identity] name_style must be one of {VALID_NAME_STYLES}")
    if cfg.identity.username_style not in VALID_USERNAME_STYLES:
        raise ConfigError(f"[identity] username_style must be one of "
                          f"{VALID_USERNAME_STYLES}")
    if cfg.dob.year_min > cfg.dob.year_max:
        raise ConfigError("[dob] year_min is after year_max")
    if not (1 <= cfg.dob.day_max <= 28):
        raise ConfigError("[dob] day_max must be 1..28 (28 dodges month length)")
    if cfg.human.mode not in VALID_HUMAN:
        raise ConfigError(f"[human] mode must be one of {VALID_HUMAN}")
    if cfg.captcha.mode not in VALID_CAPTCHA:
        raise ConfigError(f"[captcha] mode must be one of {VALID_CAPTCHA}")
    if cfg.sms.enabled and not (cfg.sms.provider and cfg.sms.key):
        raise ConfigError("[sms] enabled but provider/key are empty")
    if "." not in cfg.app.package:
        raise ConfigError("[app] package must look like com.example.app")
    if cfg.device.width < 320 or cfg.device.height < 320:
        raise ConfigError("[device] width/height look wrong")


DEFAULT_YAML = """# dc-gen-android :: configuration
run:
  count: 1
  instances: [0]
net:
  mitm: false
email:
  source: "catchall"
  domain: ""
account:
  password: "change-me"
captcha:
  mode: "human"
app:
  package: "com.discord"
"""


def write_default(path="configs/config.yaml", force=False) -> Path:
    p = Path(path)
    if p.exists() and not force:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_YAML, encoding="utf-8")
    return p