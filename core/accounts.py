"""Account records, email generation and incremental output."""
from __future__ import annotations

import csv
import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from core.names import NameFactory

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


@dataclass
class Account:
    index: int
    email: str
    password: str
    display_name: str
    username: str
    dob: dict
    instance: int = 0
    status: str = "pending"
    note: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def dob_text(self) -> str:
        return self.dob.get("text", "")

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("dob", None)
        d["dob"] = self.dob_text
        return d


def tag_for(pattern: str, index: int, first: int) -> str:
    """Resolve one gmail-plus tag.

    "dcgen{n}"  -> dcgen1, dcgen2, ...        (index substituted)
    "1"         -> 1, 2, 3, ...               (a bare number is the START)
    "acct-"     -> acct-1, acct-2, ...        (anything else is a PREFIX)

    The middle case is the one that bites: a pattern with no {n} used to be
    returned verbatim by str.format, so "1" gave +1 for every single account.
    """
    if "{n" in pattern:
        try:
            return pattern.format(n=index)
        except (KeyError, IndexError, ValueError):
            return pattern.replace("{n}", str(index))
    if pattern.isdigit():
        return str(int(pattern) + (index - first))
    return f"{pattern}{index}"


def load_emails(cfg):
    """Every address this run will use, in order."""
    start = cfg.accounts.start_index
    n = cfg.run.accounts
    if cfg.accounts.email_source == "file":
        lines = [ln.strip() for ln in
                 Path(cfg.accounts.email_file).read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if len(lines) < n:
            raise RuntimeError(
                f"{cfg.accounts.email_file} has {len(lines)} addresses, need {n}")
        return lines[:n]

    local, _, domain = cfg.accounts.email_base.partition("@")
    if not domain:
        raise RuntimeError(f"email_base {cfg.accounts.email_base!r} has no @domain")

    emails = [f"{local}+{tag_for(cfg.accounts.tag_pattern, i, start)}@{domain}"
              for i in range(start, start + n)]

    dupes = sorted({e for e in emails if emails.count(e) > 1})
    if dupes:
        raise RuntimeError(
            f"tag_pattern {cfg.accounts.tag_pattern!r} produced duplicate "
            f"addresses: {', '.join(dupes)} - use {{n}} in the pattern, "
            f"or a bare number like 1")
    return emails


def make_dob(cfg, rng) -> dict:
    y = cfg.dob.pin_year or rng.randint(cfg.dob.year_min, cfg.dob.year_max)
    m = cfg.dob.pin_month or rng.randint(1, 12)
    d = cfg.dob.pin_day or rng.randint(1, cfg.dob.day_max)
    return {"year": str(y), "month": MONTHS[m - 1], "month_num": m, "day": str(d),
            "text": f"{m:02d}/{d:02d}/{y}"}


class AccountFactory:
    """Turns a (index, instance) slot into a fully-populated Account."""

    def __init__(self, cfg, seed=None):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.names = NameFactory(cfg, self.rng)
        self.emails = load_emails(cfg)

    def make(self, slot_index: int, instance: int) -> Account:
        pos = slot_index - self.cfg.accounts.start_index
        email = self.emails[pos]
        return Account(
            index=slot_index,
            email=email,
            password=self.cfg.accounts.password,
            display_name=self.names.display_name(email),
            username=self.names.username(email),
            dob=make_dob(self.cfg, self.rng),
            instance=instance,
        )

    def commit(self):
        self.names.commit()


class AccountWriter:
    """Thread-safe incremental writer: CSV and/or JSONL, plus a credentials line."""

    def __init__(self, cfg):
        self.cfg = cfg
        out = Path(cfg.output.dir)
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.csv_path = out / f"accounts-{stamp}.csv"
        self.json_path = out / f"accounts-{stamp}.jsonl"
        self.cred_path = Path(cfg.output.credentials_file)
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._csv_header = not self.csv_path.exists()
        self._creds = set()
        if self.cred_path.exists():
            self._creds = {ln.split(":")[0] for ln in
                           self.cred_path.read_text(encoding="utf-8").splitlines() if ln}

    def write(self, acct: Account):
        row = acct.as_row()
        with self.lock:
            if self.cfg.output.format in ("csv", "both"):
                with open(self.csv_path, "a", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(row))
                    if self._csv_header:
                        w.writeheader()
                        self._csv_header = False
                    w.writerow(row)
            if self.cfg.output.format in ("json", "both"):
                with open(self.json_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            if acct.status == "ok" and acct.email not in self._creds:
                self._creds.add(acct.email)
                with open(self.cred_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{acct.email}:{acct.password}:{acct.username}\n")