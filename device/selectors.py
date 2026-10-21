"""selectors.yaml as data. No device access here - ui.py does the resolving."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {"res", "text", "text_contains", "desc", "xpath", "coord", "ocr", "foreground"}


@dataclass
class Strategy:
    by: str
    value: object

    def __post_init__(self) -> None:
        if self.by not in KINDS:
            raise ValueError(f"unknown strategy kind: {self.by!r}")


@dataclass
class Element:
    name: str
    strategies: list[Strategy]
    timeout: float
    optional: bool = False
    note: str = ""


@dataclass
class Screen:
    name: str
    detect: list[str]
    timeout: float
    priority: float = 0.0


@dataclass
class Book:
    path: Path
    meta: dict = field(default_factory=dict)
    elements: dict[str, Element] = field(default_factory=dict)
    states: dict[str, Screen] = field(default_factory=dict)

    @property
    def coordinate_space(self) -> tuple[int, int]:
        w, h = (self.meta.get("coordinate_space") or [1080, 1920])[:2]
        return int(w), int(h)

    @property
    def default_timeout(self) -> float:
        return float((self.meta.get("defaults") or {}).get("timeout", 20))

    def unverified(self) -> list[str]:
        return list(self.meta.get("unverified_ids") or [])

    def validate_unverified(self) -> list[str]:
        live = {s.value for el in self.elements.values()
                for s in el.strategies if s.by == "res"}
        return [i for i in self.unverified() if i not in live]


def load(path: Path) -> Book:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    book = Book(path=Path(path), meta=raw.get("meta") or {})
    default_timeout = book.default_timeout

    for name, spec in (raw.get("elements") or {}).items():
        spec = spec or {}
        strat = []
        for s in spec.get("strategies") or []:
            if "by" not in s or "value" not in s:
                raise ValueError(f"{path}: {name} has a malformed strategy: {s}")
            strat.append(Strategy(s["by"], s["value"]))
        if not strat:
            raise ValueError(f"{path}: {name} has no strategies")
        book.elements[name] = Element(
            name=name, strategies=strat,
            timeout=float(spec.get("timeout", default_timeout)),
            optional=bool(spec.get("optional", False)),
            note=spec.get("note", ""))

    for name, spec in (raw.get("states") or {}).items():
        spec = spec or {}
        detect = spec.get("detect") or []
        missing = [d for d in detect if d not in book.elements]
        if missing:
            raise ValueError(f"{path}: state {name} detects unknown elements {missing}")
        book.states[name] = Screen(name=name, detect=detect,
                                   timeout=float(spec.get("timeout", default_timeout)),
                                   priority=float(spec.get("priority", 0)))
    return book


def validate(book: Book) -> list[str]:
    problems = []
    for name, screen in book.states.items():
        if not screen.detect:
            problems.append(f"state {name} has no detect elements")
        for dep in screen.detect:
            if not book.elements[dep].strategies:
                problems.append(f"state {name} depends on empty element {dep}")
    seen: dict[tuple, str] = {}
    for name, screen in book.states.items():
        key = (tuple(sorted(screen.detect)), screen.priority)
        if key in seen:
            problems.append(f"states {seen[key]} and {name} are identical")
        seen[key] = name
    return problems