"""Address generation, inbox announcement, inbox opening."""
from __future__ import annotations

import hashlib
import secrets
import subprocess
import urllib.parse

from core.log import log

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_opened: set[str] = set()


def _token(n: int = 6) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def generate(cfg, store) -> str:
    src = cfg.email.source
    if src == "manual":
        path = cfg.paths.out_dir / "emails.txt"
        if not path.exists():
            raise SystemExit(f"--email-source manual needs {path}")
        known = store.known_emails()
        for addr in (l.strip() for l in path.read_text().splitlines() if l.strip()):
            if addr not in known:
                return addr
        raise SystemExit("emails.txt is exhausted")

    if src == "catchall":
        if not cfg.email.domain:
            raise SystemExit("--email-source catchall needs --email-domain")
        return f"dcgen-{_token()}@{cfg.email.domain}"

    if src == "gmail-plus":
        if not cfg.email.base:
            raise SystemExit("--email-source gmail-plus needs --email-base")
        local, _, domain = cfg.email.base.partition("@")
        return f"{local}+dcgen{_token()}@{domain or 'gmail.com'}"

    if src == "imap":
        if not cfg.email.domain:
            raise SystemExit("--email-source imap needs --email-domain")
        return f"dcgen-{_token()}@{cfg.email.domain}"

    if src == "tempmail-api":
        import requests
        dom = requests.get("https://api.mail.tm/domains", timeout=20).json()
        domain = dom["hydra:member"][0]["domain"]
        addr = f"dcgen{_token()}@{domain}"
        requests.post("https://api.mail.tm/accounts",
                      json={"address": addr, "password": "dcgen-pass"}, timeout=20)
        return addr

    raise SystemExit(f"unknown --email-source {src!r}")


def inbox_url(cfg, addr: str) -> str | None:
    if cfg.email.inbox_open == "never":
        return None
    if cfg.email.source == "gmail-plus":
        local, _, _ = addr.partition("@")
        return ("https://mail.google.com/mail/u/0/#search/"
                + urllib.parse.quote(f"to:{local}"))
    if cfg.email.source in ("catchall", "imap"):
        return cfg.email.inbox_search.replace("{addr}", urllib.parse.quote(addr))
    if cfg.email.source == "tempmail-api":
        return "https://mail.tm"
    return None


def announce(cfg, addr: str) -> None:
    print(f"\n  >>  {addr}  <<\n", flush=True)
    log(f"email: {addr}")


def open_inbox(cfg, addr: str) -> None:
    if addr in _opened:
        return
    url = inbox_url(cfg, addr)
    if not url:
        return
    subprocess.run(["open", url], check=False)
    _opened.add(addr)
    log(f"opened inbox: {url}")


def fingerprint(addr: str) -> str:
    return hashlib.sha1(addr.encode()).hexdigest()[:8]