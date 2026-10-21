"""Every filesystem location a run touches, in one place.

Nothing else in the tree hardcodes a path. Store, AvdBackend, Device and the
orchestrator all read from here, so relocating output is a one-line edit.

Layout (under output.dir, which defaults to ./outputs):

    tokens.txt        one token per line
    evs.txt           email:password:token:user_id
    pending.txt       email:password:token:instance:epoch:state
    locked.txt        same shape, for the phone wall
    report.csv        run report
    credentials.txt   email:password
    run.lock          single-writer guard
    state/            per-account JSON records
    shots/            failure screenshots + raw dumps
    logs/             emulator-<i>.log, mitm-<i>.log
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SELECTOR_CANDIDATES = ("configs/selectors.yaml", "config/selectors.yaml",
                       "selectors.yaml")


@dataclass
class Paths:
    root: Path = field(default_factory=Path.cwd)
    out_dir: Path | None = None
    selectors_file: Path | None = None
    credentials_file: Path | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.out_dir = (Path(self.out_dir) if self.out_dir
                        else self.root / "outputs").resolve()
        if self.selectors_file is None:
            for cand in SELECTOR_CANDIDATES:
                p = self.root / cand
                if p.exists():
                    self.selectors_file = p
                    break
            else:
                self.selectors_file = self.root / SELECTOR_CANDIDATES[0]
        self.selectors_file = Path(self.selectors_file)

    # ---- directories -----------------------------------------------------
    @property
    def state(self) -> Path:
        return self.out_dir / "state"

    @property
    def shots(self) -> Path:
        return self.out_dir / "shots"

    @property
    def logs(self) -> Path:
        return self.out_dir / "logs"

    # ---- files -----------------------------------------------------------
    @property
    def tokens(self) -> Path:
        return self.out_dir / "tokens.txt"

    @property
    def evs(self) -> Path:
        return self.out_dir / "evs.txt"

    @property
    def pending(self) -> Path:
        return self.out_dir / "pending.txt"

    @property
    def locked(self) -> Path:
        return self.out_dir / "locked.txt"

    @property
    def report(self) -> Path:
        return self.out_dir / "report.csv"

    @property
    def credentials(self) -> Path:
        return Path(self.credentials_file) if self.credentials_file \
            else self.out_dir / "credentials.txt"

    @property
    def run_lock(self) -> Path:
        return self.out_dir / "run.lock"

    @property
    def selectors(self) -> Path:
        return self.selectors_file

    def ensure(self) -> "Paths":
        for d in (self.out_dir, self.state, self.shots, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def as_dict(self) -> dict[str, str]:
        return {"root": str(self.root), "out_dir": str(self.out_dir),
                "logs": str(self.logs), "selectors": str(self.selectors)}