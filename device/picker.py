"""DOB picker wheel: one row per gesture, tuned per column.

read() is BAND-LIMITED (y 930..1090) so it can only ever read the centre row of a
picker column - a column can never latch onto a row above or below the selected
one.

spin() moves ONE ROW PER GESTURE. The drag spans exactly one row pitch (114px,
the picker's own spacing) so the snap lands on the adjacent item instead of a
fling carrying three. Duration is tuned per column: too fast overshoots, too
slow is swallowed, both bump it by 40ms. Two swallowed drags fall back to a tap.

read() returning None for 8 consecutive polls is what a CLOSED modal looks like,
not a stuck wheel, so that is what raises.
"""
from __future__ import annotations

import time

from core.errors import DCGenError, FailureCode
from device.driver import Device

WHEEL_CENTRE_Y = 1009
WHEEL_ROW_PITCH = 114            # one row == one gesture
WHEEL_CENTRE_BAND = (930, 1090)  # only the selected row is readable
WHEEL_COL_TOLERANCE = 80         # px - neighbouring columns are ~98 apart
WHEEL_FAST = 60                  # ms - starting drag duration
WHEEL_SLOW = 220                 # ms - cap
WHEEL_TUNE_STEP = 40             # ms - bump per correction
WHEEL_SETTLE = 0.08              # s - let the snap finish

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")

_TUNE: dict[int, int] = {}


def _rank(v) -> int:
    """Ordinal for direction/equality. Handles '2003', '03' and 'March'."""
    if v is None:
        return 0
    v = str(v).strip()
    if v.isdigit():
        return int(v)
    return MONTHS.index(v) + 1 if v in MONTHS else 0


def _same(cur, target) -> bool:
    if cur is None:
        return False
    if str(cur).strip() == str(target).strip():
        return True
    r = _rank(target)
    return r != 0 and _rank(cur) == r


def read(dev, x: int, band=WHEEL_CENTRE_BAND) -> str | None:
    """Centre row of the column at x. Falls back to any node in the band."""
    fallback = None
    for n in Device.nodes(dev.dump()):
        c = Device.centre_of(n)
        if c is None or abs(c[0] - x) > WHEEL_COL_TOLERANCE:
            continue
        if not (band[0] <= c[1] <= band[1]):
            continue
        text = (n.get("text") or "").strip()
        if (n.get("class") or "") == "android.widget.EditText":
            return text
        if fallback is None and text:
            fallback = text
    return fallback


def _step(dev, x: int, up: bool, duration: int, pitch: int = WHEEL_ROW_PITCH) -> None:
    """The drag starts and ends inside the picker's touch area (y 838..1180)."""
    half = pitch // 2
    y1 = WHEEL_CENTRE_Y + half if up else WHEEL_CENTRE_Y - half
    y2 = WHEEL_CENTRE_Y - half if up else WHEEL_CENTRE_Y + half
    dev.swipe(x, y1, x, y2, duration)


def _nudge_row(dev, x: int, cur, target) -> None:
    """Instant fallback: tap the row one step toward the target."""
    rows: dict[int, tuple[int, int]] = {}
    for n in Device.nodes(dev.dump()):
        c = Device.centre_of(n)
        if c is None or abs(c[0] - x) > WHEEL_COL_TOLERANCE:
            continue
        if (n.get("class") or "") == "android.widget.Button":
            rows[c[1]] = c
    if not rows:
        return
    ys = sorted(rows)
    pick = rows[ys[-1]] if _rank(target) > _rank(cur) else rows[ys[0]]
    dev.tap_xy(*pick)


def spin(dev, x: int, target, max_steps: int = 120) -> int:
    """Advance the column at x one row per gesture. Returns the step count."""
    duration = _TUNE.get(x, WHEEL_FAST)
    cur = read(dev, x)
    blind = swallowed = 0

    for step in range(max_steps):
        if _same(cur, target):
            _TUNE[x] = duration
            return step
        if cur is None:
            blind += 1
            if blind >= 8:
                raise DCGenError(FailureCode.E_DOB_FAIL,
                                 f"no readable row at x={x} (modal closed?)")
            time.sleep(0.2)
            cur = read(dev, x)
            continue
        blind = 0

        up = _rank(target) > _rank(cur)
        before = _rank(cur)
        _step(dev, x, up, duration)
        time.sleep(WHEEL_SETTLE)
        prev, cur = cur, read(dev, x)

        if cur is None:
            continue
        moved = abs(_rank(cur) - before)
        if moved == 0:
            swallowed += 1
            duration = min(WHEEL_SLOW, duration + WHEEL_TUNE_STEP)
            _TUNE[x] = duration
            if swallowed >= 2:
                _nudge_row(dev, x, prev, target)
                time.sleep(WHEEL_SETTLE)
                cur = read(dev, x)
                swallowed = 0
        elif moved > 1:
            # fling carried extra rows - a slower release settles on one
            duration = min(WHEEL_SLOW, duration + WHEEL_TUNE_STEP)
            _TUNE[x] = duration
        else:
            swallowed = 0

    raise DCGenError(FailureCode.E_DOB_FAIL,
                     f"wheel x={x} stuck on {cur!r} (wanted {target!r})")