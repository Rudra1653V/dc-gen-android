"""Semantic UI layer: resolves selectors.yaml strategy chains against a live dump.

Strategy order is fixed and never reordered:
    res -> text -> text_contains -> desc -> xpath -> coord -> ocr

Includes a minimal, self-contained xpath subset so this file has no dependency
on device/xpath.py:

    //Tag                     //*                     //Tag[3]      //Tag[last()]
    //*[@attr='v']            //*[contains(@attr,'v')]
    //*[contains(@text,'a') or contains(@text,'b')]
    (//Tag[@x='y'])[2]

`coord` hit-tests the dump: it returns None unless a visible node actually
contains the point, so a coordinate can never "match" empty pixels.
"""
from __future__ import annotations

import re
import time

from core.errors import DCGenError, FailureCode
from core.log import log
from device.driver import Found
from device import ocr

_TAG_RE = re.compile(r"^[A-Za-z_][\w.]*$")
_CLAUSE = re.compile(r"@([\w-]+)\s*=\s*'([^']*)'"
                     r"|contains\(\s*@([\w-]+)\s*,\s*'([^']*)'\s*\)")


def _tag_of(node) -> str:
    """uiautomator puts every widget in <node> with the class in an attribute."""
    return node.get("class") or node.tag


def _clause_ok(node, clause: str) -> bool:
    m = _CLAUSE.fullmatch(clause.strip())
    if not m:
        return False
    if m.group(1) is not None:
        return (node.get(m.group(1)) or "") == m.group(2)
    return m.group(4).lower() in (node.get(m.group(3)) or "").lower()


def _pred_ok(node, preds: str) -> bool:
    for or_part in preds.split(" or "):
        if all(_clause_ok(node, c) for c in or_part.split(" and ")):
            return True
    return False


def _visible(node) -> bool:
    b = node.get("bounds") or ""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
    if not m:
        return False
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return x2 > x1 and y2 > y1


def _eval_xpath(root, expr: str) -> list:
    e = expr.strip()
    idx = None
    if e.startswith("("):
        close = e.rfind(")")
        if close < 0:
            return []
        tail = e[close + 1:].strip()
        if tail.startswith("[") and tail.endswith("]"):
            idx = tail[1:-1]
        e = e[1:close].strip()
    if not e.startswith("//"):
        return []
    e = e[2:]
    i = 0
    while i < len(e) and (e[i].isalnum() or e[i] in "._*"):
        i += 1
    tag = e[:i] or "*"
    rest = e[i:].strip()
    preds = None
    if rest:
        if not (rest.startswith("[") and rest.endswith("]")):
            return []
        preds = rest[1:-1].strip()
    if preds is not None and re.fullmatch(r"\d+", preds):
        idx, preds = preds, None
    elif preds is not None and preds.startswith("last()"):
        idx, preds = "last()", None

    nodes = [n for n in root.iter() if tag == "*" or _tag_of(n) == tag]
    if preds:
        nodes = [n for n in nodes if _pred_ok(n, preds)]
    if idx == "last()":
        return nodes[-1:] if nodes else []
    if idx and idx.isdigit():
        k = int(idx)
        return nodes[k - 1:k] if 0 < k <= len(nodes) else []
    return nodes


class Ui:
    def __init__(self, device, book):
        self.device = device
        self.book = book
        self._png: bytes | None = None
        self._png_fresh = False

    # ---- resolution ------------------------------------------------------
    def _screen(self) -> bytes:
        """One screencap per dump generation, not one per ocr strategy."""
        if self.device.dump_is_fresh and self._png is not None:
            return self._png
        self._png = self.device.screenshot_png()
        return self._png

    def _hit_test(self, root, x: int, y: int):
        """Smallest visible node containing the point; None if empty pixels."""
        best = None
        best_area = None
        for node in root.iter("node"):
            b = self.device.bounds_of(node)
            if not b:
                continue
            x1, y1, x2, y2 = b
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                continue
            area = (x2 - x1) * (y2 - y1)
            if best_area is None or area < best_area:
                best, best_area = node, area
        return best

    def _scaled_coord(self, value) -> tuple[int, int]:
        space = (self.book.meta.get("coordinate_space") or [1080, 1920])
        w, h = self.device.screen_size()
        return (int(value[0] * w / space[0]), int(value[1] * h / space[1]))

    def _try(self, strat, root) -> Found | None:
        by, val = strat.by, strat.value

        if by == "coord":
            x, y = self._scaled_coord(val)
            node = self._hit_test(root, x, y)
            return Found("coord", val, x, y, node) if node is not None else None

        if by == "ocr":
            hit = ocr.find_text(self._screen(), str(val))
            if not hit:
                return None
            return Found("ocr", val, hit[0], hit[1], None)

        if by == "xpath":
            expr = str(val)
        elif by == "res":
            expr = f"//*[@resource-id='{val}']"
        elif by == "text":
            expr = f"//*[@text='{val}']"
        elif by == "text_contains":
            expr = f"//*[contains(@text,'{val}')]"
        elif by == "desc":
            expr = f"//*[@content-desc='{val}']"
        else:
            return None

        for node in _eval_xpath(root, expr):
            if not _visible(node):
                continue
            c = self.device.centre_of(node)
            if c is None:
                continue
            return Found(by, val, c[0], c[1], node)
        return None

    def find(self, name: str) -> Found | None:
        el = self.book.elements.get(name)
        if el is None:
            raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                             f"unknown element {name!r}")
        for attempt in range(2):
            try:
                root = self.device.dump()
            except DCGenError:
                if attempt:
                    raise
                self.device.invalidate()
                time.sleep(0.5)
                continue
            for strat in el.strategies:
                hit = self._try(strat, root)
                if hit:
                    return hit
            return None
        return None

    def wait(self, name: str, timeout: float | None = None) -> Found:
        el = self.book.elements[name]
        limit = timeout or el.timeout
        deadline = time.time() + limit
        last_err = None
        while time.time() < deadline:
            try:
                hit = self.find(name)
            except DCGenError as exc:       # bad dump: keep polling, do not abort
                last_err, hit = exc, None
            if hit:
                return hit
            time.sleep(self.book.poll)
        self.device.capture_failure(f"wait_{name}", f"element {name!r} never resolved")
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                         f"element {name!r} not found in {limit}s"
                         + (f" (last dump error: {last_err})" if last_err else ""))

    def exists(self, name: str) -> bool:
        if name not in self.book.elements:
            return False
        try:
            return self.find(name) is not None
        except DCGenError:
            return False

    # ---- actions ---------------------------------------------------------
    def tap(self, name: str, timeout: float | None = None) -> Found:
        hit = self.wait(name, timeout)
        self.device.tap_xy(hit.x, hit.y)
        log(f"tapped {name} via {hit.by}")
        return hit

    def tap_if(self, name: str, timeout: float | None = None) -> bool:
        if not self.exists(name):
            return False
        try:
            self.tap(name, timeout)
            return True
        except DCGenError:
            return False

    def type_into(self, name: str, value: str) -> None:
        hit = self.wait(name)
        self.device.tap_xy(hit.x, hit.y)
        time.sleep(0.3)
        self.device.type_text(value)
        log(f"typed into {name}")

    def type_if(self, name: str, value: str) -> bool:
        """type_into that tolerates an unresolvable field (not yet grounded)."""
        if not self.exists(name):
            log(f"field {name!r} absent - skipped", "warn")
            return False
        self.type_into(name, value)
        return True

    # ---- states ----------------------------------------------------------
    def state(self) -> str | None:
        try:
            matches = [n for n, s in self.book.states.items()
                       if all(self.exists(d) for d in s.detect)]
        except DCGenError:
            return None
        if not matches:
            return None
        return max(matches, key=lambda n: (self.book.states[n].priority,
                                           len(self.book.states[n].detect)))

    def wait_state(self, want: str | tuple[str, ...],
                   timeout: float | None = None) -> str:
        wants = (want,) if isinstance(want, str) else want
        limit = timeout or self.book.states[wants[0]].timeout
        deadline = time.time() + limit
        seen: set[str] = set()
        while time.time() < deadline:
            current = self.state()
            if current:
                seen.add(current)
            if current in wants:
                return current
            time.sleep(0.5)
        self.device.capture_failure(f"state_{'_'.join(wants)}",
                                    f"wanted {wants}, saw {sorted(seen)}")
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                         f"never reached state {wants}; saw {sorted(seen)}")

    def wait_gone(self, name: str, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.exists(name):
                return
            time.sleep(0.4)
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                         f"{name!r} still present after {timeout}s")