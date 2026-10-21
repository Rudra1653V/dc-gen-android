"""Clear the notification shade / keyguard before any press or snap.

Both are SEPARATE windows. A pulled-down shade swallows every tap and makes
state() report [], and a cold boot opens the shade by itself. BACK alone does
NOT reliably close it, so this walks an escalation ladder instead of guessing.

R9: never press BACK blind - only while the shade is the focused window.
"""
from __future__ import annotations

import time

SHADE_WINDOWS = ("NotificationShade", "StatusBar")
LOCK_WINDOWS = ("Keyguard", "Dream")


def shade_open(dev) -> bool:
    return any(w in dev.foreground(ttl=0.0) for w in SHADE_WINDOWS)


def locked(dev) -> bool:
    return any(w in dev.foreground(ttl=0.0) for w in LOCK_WINDOWS)


def _nudge(dev, rung: int) -> None:
    if rung == 0:
        dev.shell("cmd statusbar collapse", check=False)
    elif rung == 1:
        dev.shell("input keyevent 4", check=False)      # BACK
    elif rung == 2:
        dev.swipe(540, 1860, 540, 1100, 250)            # drag the handle up
    else:
        dev.shell("input keyevent 3", check=False)      # HOME drops the shade


def ensure_ready(dev, tries: int = 6) -> bool:
    """Returns False if it could not clear - the caller then escalates to
    HOME + relaunch rather than dying."""
    for rung in range(tries):
        focus = dev.foreground(ttl=0.0)
        if any(w in focus for w in SHADE_WINDOWS):
            _nudge(dev, rung)
            time.sleep(1.0)
            continue
        if any(w in focus for w in LOCK_WINDOWS):
            dev.shell("wm dismiss-keyguard", check=False)
            dev.swipe(540, 1500, 540, 600, 200)
            time.sleep(1.0)
            continue
        dev.invalidate()
        return True
    return False