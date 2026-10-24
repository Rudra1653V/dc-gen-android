#!/usr/bin/env python3
"""Proves the coord/state fix against synthetic dumps. No AVD, no adb.

    python tools/selftest.py        # exit 0 = fixed, exit 1 = regressed
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device.ui import Ui

LAUNCHER = """<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"/>
  <node class="android.widget.TextView" text="Camera" bounds="[420,600][660,680]"/>
  <node class="android.widget.TextView" text="Discord" bounds="[420,1480][660,1560]"/>
</hierarchy>"""

REGISTER = """<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"/>
  <node class="android.widget.EditText" text="Email" bounds="[80,580][1000,700]"/>
  <node class="android.widget.EditText" text="Password" bounds="[80,1000][1000,1120]"/>
</hierarchy>"""

ONBOARDING = """<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]"/>
  <node class="android.widget.TextView" text="Welcome to Discord" bounds="[100,400][980,520]"/>
  <node class="android.widget.Button" text="Next" bounds="[340,1680][740,1760]"/>
</hierarchy>"""

EMPTY = """<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][10,10]"/>
</hierarchy>"""

SCALED_COORD = """<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][2160,3840]"/>
  <node class="android.widget.Button" text="Scaled target"
        bounds="[900,1700][1300,2100]"/>
</hierarchy>"""

LAUNCHER_PKG = "com.google.android.apps.nexuslauncher"
SYSTEM_PKG = "com.android.systemui"


class FakeBook:
    coordinate_space = (1080, 1920)
    default_timeout = 5
    poll = 0.4
    path = "<selftest>"

    def __init__(self, states, elements):
        self.states = states
        self.elements = elements


class FakeScreen:
    def __init__(self, name, detect, priority, require_all=False):
        self.name, self.detect, self.priority, self.timeout = name, detect, priority, 5
        self.require_all = require_all


class FakeEl:
    def __init__(self, name, strategies, timeout=5):
        self.name = name
        self.strategies = strategies
        self.timeout = timeout
        self.optional = False


class FakeStrategy:
    def __init__(self, by, value, nth=None):
        self.by, self.value, self.nth = by, value, nth


class FakeDevice:
    def __init__(self, xml: str, foreground_pkg: str, size=(1080, 1920)):
        self._root = ET.fromstring(xml)
        self._fg = foreground_pkg
        self._size = size
        self._cache = (1.0, self._root)

    @property
    def dump_generation(self) -> float:
        return self._cache[0]

    @property
    def dump_is_fresh(self) -> bool:
        return True

    def dump(self, cached: bool = True):
        return self._root

    def screen_size(self):
        return self._size

    def foreground(self, ttl: float = 1.0):
        return self._fg

    def screenshot_png(self):
        return b""

    def capture_failure(self, state, note=""):
        pass

    def invalidate(self):
        pass


def _book() -> FakeBook:
    states = {
        "launcher":      FakeScreen("launcher", ["on_launcher"], 5),
        "onboarding":    FakeScreen("onboarding", ["onboarding_marker"], 10),
        "register_form": FakeScreen("register_form", ["form_marker"], 20),
        "strict_pair":   FakeScreen("strict_pair",
                                    ["onboarding_marker", "form_marker"], 30,
                                    require_all=True),
    }
    elements = {
        "on_launcher": FakeEl(
            "on_launcher", [FakeStrategy("foreground", "launcher")]),
        "onboarding_marker": FakeEl(
            "onboarding_marker",
            [FakeStrategy("text", "Next"),
             FakeStrategy("xpath", "//android.widget.Button[@text='Next']")]),
        "form_marker": FakeEl(
            "form_marker", [FakeStrategy("xpath", "//android.widget.EditText")]),
        "email_field": FakeEl(
            "email_field",
            [FakeStrategy("xpath",
                          "//android.widget.EditText[contains(@text,'Email')]"),
             FakeStrategy("coord", [540, 620])]),
        "second_edit": FakeEl(
            "second_edit",
            [FakeStrategy("xpath", "//android.widget.EditText", nth=1)]),
        "scaled_coord": FakeEl(
            "scaled_coord", [FakeStrategy("coord", [540, 960])]),
    }
    return FakeBook(states, elements)


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILURES.append(label)


def _last_edittext(ui: Ui) -> str:
    from device.driver import Device
    from device import xpath
    nodes = [n for n in Device.nodes(ui.device.dump()) if Device.visible(n)]
    hits = xpath.match("(//android.widget.EditText)[last()]", nodes)
    return Device.label(hits[0]) if hits else ""


def main() -> int:
    print("coord / state regression test\n")

    print("1. coord hit-tests the dump (nothing under the point -> no match)")
    ui = Ui(FakeDevice(EMPTY, SYSTEM_PKG), _book())
    check("exists('email_field') on an empty screen", ui.exists("email_field"), False)
    check("state() on an empty screen", ui.state(), None)

    print("\n2. state() is not fooled by a coord fallback")
    ui = Ui(FakeDevice(LAUNCHER, LAUNCHER_PKG), _book())
    check("state() on the launcher", ui.state(), "launcher")
    check("exists('email_field') on the launcher is a coord false-positive",
          ui.exists("email_field"), True)
    check("...but exists_strict() rejects it",
          ui.exists_strict("email_field"), False)

    print("\n3. real screens still identify correctly")
    ui = Ui(FakeDevice(REGISTER, "com.discord"), _book())
    check("state() on the register form", ui.state(), "register_form")

    ui = Ui(FakeDevice(ONBOARDING, "com.discord"), _book())
    check("state() on onboarding", ui.state(), "onboarding")

    print("\n4. require_all needs every detect element")
    check("require_all state absent on onboarding",
          "strict_pair" in (ui.state() or ""), False)

    print("\n5. confidence flag and xpath semantics")
    ui = Ui(FakeDevice(LAUNCHER, LAUNCHER_PKG), _book())
    hit = ui.find("email_field")
    check("coord fallback resolves on the launcher", hit is not None, True)
    check("...and is marked not confident", hit.confident, False)

    ui = Ui(FakeDevice(REGISTER, "com.discord"), _book())
    check("xpath hit is confident", ui.find("email_field").confident, True)
    check("(//TAG)[last()] resolves to the last match", _last_edittext(ui), "Password")
    check("nth=1 picks the second EditText",
          ui.find("second_edit").y > ui.find("email_field").y, True)

    print("\n6. coordinate fallbacks scale from the reference screen")
    ui = Ui(FakeDevice(SCALED_COORD, "com.discord", size=(2160, 3840)),
            _book())
    hit = ui.find("scaled_coord")
    check("scaled coord resolves on a 2x screen", hit is not None, True)
    check("scaled coord uses the physical screen centre",
          (hit.x, hit.y) if hit else None, (1080, 1920))

    print()
    if FAILURES:
        print(f"REGRESSED: {len(FAILURES)} check(s) failed -> {FAILURES}")
        return 1
    print("all checks passed - the coord/state fix is in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())