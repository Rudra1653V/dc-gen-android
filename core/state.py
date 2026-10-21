"""Account state machine and crash-safe output files.

    tokens.txt    <token>
    evs.txt       <email>:<password>:<token>:<user_id>
    pending.txt   <email>:<password>:<token>:<instance>:<epoch>:<state>
    locked.txt    same shape (phone wall)

Promotion to evs.txt is idempotent by email, so a crash between promote and
drop can never duplicate a line.
"""
from __future__ import annotations

import csv
import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from core.errors import FailureCode

PENDING_TTL_H = 24


class State(str, Enum):
    NEW = "NEW"
    PREPARED = "PREPARED"
    FORM_FILLED = "FORM_FILLED"
    SUBMITTED = "SUBMITTED"
    CAPTCHA = "CAPTCHA"
    TOKEN_CAPTURED = "TOKEN_CAPTURED"
    EMAIL_VERIFY_PENDING = "EMAIL_VERIFY_PENDING"
    VERIFIED = "VERIFIED"
    PHONE_LOCKED = "PHONE_LOCKED"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL = {State.DONE, State.FAILED}
PARKED = {State.CAPTCHA, State.EMAIL_VERIFY_PENDING, State.PHONE_LOCKED}


@dataclass
class Account:
    email: str
    password: str
    username: str = ""
    display_name: str = ""
    token: str = ""
    user_id: str = ""
    instance: int = -1
    state: State = State.NEW
    created: float = field(default_factory=time.time)
    proxy_ip: str = ""
    failure: str = ""
    detail: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    attempts: int = 0

    def mark(self, state: State) -> None:
        self.state = state
        self.timings[state.value] = round(time.time() - self.created, 2)

    def fail(self, code: FailureCode, detail: str = "") -> None:
        self.state = State.FAILED
        self.failure = code.value
        self.detail = detail[:400]

    def pending_line(self) -> str:
        return ":".join([self.email, self.password, self.token,
                         str(self.instance), str(int(self.created)), self.state.value])

    def evs_line(self) -> str:
        return ":".join([self.email, self.password, self.token, self.user_id])

    def age_h(self) -> float:
        return (time.time() - self.created) / 3600


class Store:
    def __init__(self, paths):
        self.paths = paths
        paths.ensure()

    @staticmethod
    def _append(path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line.rstrip("\n") + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _rewrite(path: Path, lines: list[str]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(l.rstrip("\n") + "\n" for l in lines), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _emails_in(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {l.split(":", 1)[0] for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip()}

    def save_token(self, acct: Account) -> None:
        self._append(self.paths.tokens, acct.token)

    def save_evs(self, acct: Account) -> None:
        if acct.email in self._emails_in(self.paths.evs):
            return
        self._append(self.paths.evs, acct.evs_line())

    def save_record(self, acct: Account) -> None:
        rec = asdict(acct)
        rec["state"] = acct.state.value
        (self.paths.state / f"{acct.email}.json").write_text(
            json.dumps(rec, indent=2), encoding="utf-8")

    def park(self, acct: Account, queue: str = "pending") -> None:
        path = self.paths.pending if queue == "pending" else self.paths.locked
        if acct.email in self._emails_in(path):
            self.save_record(acct)
            return
        self._append(path, acct.pending_line())
        self.save_record(acct)

    def read_queue(self, queue: str = "pending") -> list[Account]:
        path = self.paths.pending if queue == "pending" else self.paths.locked
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) < 3:
                continue
            email, password, token = parts[0], parts[1], parts[2]
            inst = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else -1
            created = float(parts[4]) if len(parts) > 4 and parts[4].isdigit() else time.time()
            state = State(parts[5]) if len(parts) > 5 and parts[5] in State.__members__ \
                else State.EMAIL_VERIFY_PENDING
            out.append(Account(email=email, password=password, token=token,
                               instance=inst, created=created, state=state))
        return out

    def rewrite_queue(self, accounts: list[Account], queue: str = "pending") -> None:
        path = self.paths.pending if queue == "pending" else self.paths.locked
        self._rewrite(path, [a.pending_line() for a in accounts])

    def drop_from_queue(self, email: str, queue: str = "pending") -> None:
        left = [a for a in self.read_queue(queue) if a.email != email]
        self.rewrite_queue(left, queue)

    def known_emails(self) -> set[str]:
        emails = self._emails_in(self.paths.pending) | self._emails_in(self.paths.locked)
        return emails | self._emails_in(self.paths.evs)

    def stale(self, queue: str = "pending") -> list[Account]:
        return [a for a in self.read_queue(queue) if a.age_h() > PENDING_TTL_H]

    def all_records(self) -> list[Account]:
        out: list[Account] = []
        if not self.paths.state.exists():
            return out
        for path in sorted(self.paths.state.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except Exception:
                continue
            raw["state"] = State(raw.get("state", "FAILED"))
            out.append(Account(**{k: v for k, v in raw.items()
                                  if k in Account.__dataclass_fields__}))
        return out

    def write_report(self, accounts: list[Account] | None = None) -> None:
        rows = accounts if accounts is not None else self.all_records()
        with open(self.paths.report, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["email", "username", "instance", "state", "failure", "detail",
                        "proxy_ip", "age_s", "timings"])
            for a in rows:
                w.writerow([a.email, a.username, a.instance, a.state.value, a.failure,
                            a.detail, a.proxy_ip, round(time.time() - a.created, 1),
                            json.dumps(a.timings)])


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self, force: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.seek(0)
            owner = self.fh.read().strip() or "unknown"
            if not force:
                raise SystemExit(f"another run holds {self.path} (pid {owner}). "
                                 "Use --force or wait.")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()

    def release(self) -> None:
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()