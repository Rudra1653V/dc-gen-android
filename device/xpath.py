"""Tiny xpath subset matcher for uiautomator dumps.

IMPORTANT: in a uiautomator dump every widget is a <node> element and the widget
class lives in the `class` ATTRIBUTE, not the tag:

    <node class="android.widget.EditText" text="Email" bounds="[80,580][1000,700]"/>

so //android.widget.EditText must match on @class. ElementTree's own xpath also
has no contains(), which is why this exists.
"""
from __future__ import annotations

import re

_ATTR_EQ = re.compile(
    r"^\(?//(?P<tag>[\w.*]+)"
    r"(?:\[@(?P<attr>[\w-]+)=(?P<q>['\"])(?P<val>.*?)(?P=q)\])?"
    r"(?:\[(?P<idx>\d+|last\(\))\])?"
    r"\)?(?:\[(?P<idx2>\d+|last\(\))\])?$")
_ATTR_HAS = re.compile(
    r"^//(?P<tag>[\w.*]+)\[contains\(@(?P<attr>[\w-]+),\s*(?P<q>['\"])(?P<val>.*?)(?P=q)\)\]$")

WILDCARD = ("*", "node")


def _tag_of(node) -> str:
    if not isinstance(node.tag, str):
        return ""
    return node.get("class") or node.tag


def _tag_matches(tag: str, node) -> bool:
    return True if tag in WILDCARD else _tag_of(node) == tag


def _pick(hits: list, idx: str | None) -> list:
    if not idx:
        return hits
    if idx == "last()":
        return hits[-1:]
    pos = int(idx)
    return hits[pos - 1:pos] if pos >= 1 else []


def match(expr: str, nodes: list) -> list:
    expr = expr.strip()

    m = _ATTR_HAS.match(expr)
    if m:
        tag, attr, needle = m.group("tag"), m.group("attr"), m.group("val")
        return [n for n in nodes
                if _tag_matches(tag, n) and needle in (n.get(attr) or "")]

    m = _ATTR_EQ.match(expr)
    if m:
        tag, attr, val = m.group("tag"), m.group("attr"), m.group("val")
        hits = [n for n in nodes
                if _tag_matches(tag, n)
                and (attr is None or (n.get(attr) or "") == val)]
        return _pick(hits, m.group("idx") or m.group("idx2"))

    raise ValueError(f"unsupported xpath: {expr!r}")