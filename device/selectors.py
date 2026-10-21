"""selectors.yaml as data. No device access here - ui.py does the resolving.

Strategy keys are accepted as either `kind:` (what the shipped selectors.yaml
uses) or `by:`. Every kind selectors.yaml relies on is declared in KINDS, so a
typo fails at load rather than silently never matching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {
    "res", "text", "text_contains", "desc", "desc_contains", "label",
    "label_contains", "class", "xpath", "coord", "ocr", "foreground",
}


@dataclass
class Strategy:
    by: str
    value: object
    nth: int | None = None

    def __post_init__(self) -> None:
        if self.by not in KINDS:
            raise ValueError(f"unknown strategy kind: {self.by!r}")


@dataclass
class Element:
    name: str
    strategies: list
    timeout: float
    optional: bool = False
    note: str = ""


@dataclass
class Screen:
    name: str
    detect: list
    timeout: float
    priority: float = 0.0
    require_all: bool = False


@dataclass
class Book:
    path: Path
    meta: dict = field(default_factory=dict)
    elements: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)

    @property
    def coordinate_space(self) -> tuple[int, int]:
        space = self.meta.get("coordinate_space")
        if not space:
            screen = self.meta.get("screen") or {}
            if screen.get("w") and screen.get("h"):
                space = [screen["w"], screen["h"]]
        w, h = (space or [1080, 1920])[:2]
        return int(w), int(h)

    @property
    def default_timeout(self) -> float:
        return float((self.meta.get("defaults") or {}).get("timeout", 20))

    @property
    def poll(self) -> float:
        return float((self.meta.get("defaults") or {}).get("poll", 0.4))

    def unverified(self) -> list:
        return list(self.meta.get("unverified_ids") or [])

    def validate_unverified(self) -> list:
        live = {s.value for el in self.elements.values()
                for s in el.strategies if s.by == "res"}
        return [i for i in self.unverified() if i not in live]


def _strategy(raw: dict, where: str) -> Strategy:
    by = raw.get("by") or raw.get("kind")
    if by is None or "value" not in raw:
        raise ValueError(f"{where}: malformed strategy: {raw}")
    nth = raw.get("nth")
    return Strategy(by, raw["value"], int(nth) if nth is not None else None)


def load(path) -> Book:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    book = Book(path=path, meta=raw.get("meta") or {})
    if "screen" in raw and "screen" not in book.meta:
        book.meta["screen"] = raw["screen"]
    default_timeout = book.default_timeout

    for name, spec in (raw.get("elements") or {}).items():
        spec = spec or {}
        strat = [_strategy(s, f"{path}: {name}")
                 for s in (spec.get("strategies") or [])]
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
        book.states[name] = Screen(
            name=name, detect=detect,
            timeout=float(spec.get("timeout", default_timeout)),
            priority=float(spec.get("priority", 0)),
            require_all=bool(spec.get("require_all", False)))
    return book


def validate(book: Book) -> list:
    problems = []
    for name, screen in book.states.items():
        if not screen.detect:
            problems.append(f"state {name} has no detect elements")
        for dep in screen.detect:
            if not book.elements[dep].strategies:
                problems.append(f"state {name} depends on empty element {dep}")
    seen: dict = {}
    for name, screen in book.states.items():
        key = (tuple(sorted(screen.detect)), screen.priority, screen.require_all)
        if key in seen:
            problems.append(f"states {seen[key]} and {name} are identical")
        seen[key] = name
    return problems