"""Low-level AVD control + accessibility selector engine for dc-gen-android.

Invariants, each bought with a real failure:

  1. dump() returns parseable XML or raises.  `uiautomator dump` wraps its
     payload in a "UI hierchary dumped to:" line and React Native emits no
     <?xml declaration, so we slice on the OUTERMOST angle brackets rather
     than searching for '<?xml' (find returns -1, and [-1:] slices nothing).

  2. start_app() verifies the package owns the foreground window.  `am start`
     fails silently, and then every tap lands on the Android home screen while
     state() still reports plausible screens.

  3. ensure_ready() runs before every press and every snap.  A pulled-down
     notification shade (or the keyguard) is a SEPARATE window: it swallows
     every tap and makes state() report [].  A cold boot opens the shade by
     itself and BACK alone does NOT reliably close it, so ensure_ready walks an
     escalation ladder: `cmd statusbar collapse`, BACK, handle swipe, HOME.

  4. ensure_online() runs before anything else.  An unattached or still-booting
     emulator makes every adb call return '', which surfaces as a misleading
     E_APP_NOT_FOREGROUND rather than E_DEVICE_OFFLINE.

  5. resolve(strict_coord=True) makes a coord that contains no node count as a
     MISS.  Without it, a coord fallback fabricates a node and the element
     looks PERMANENTLY PRESENT.  present() and wait_gone() are therefore strict.

  6. read_wheel() is BAND-LIMITED (y 930..1090) so it can only ever read the
     centre row of a picker column.  wheel() raises E_DOB_WHEEL_GONE if the
     column is unreadable for 8 consecutive polls - which is what a CLOSED
     modal looks like, not a stuck wheel.

  7. wheel() moves ONE ROW PER GESTURE.  The drag is exactly one row pitch
     (114px, the picker's own spacing) so the snap lands on the adjacent item
     instead of the fling carrying three.  Duration is tuned per column: too
     fast overshoots, too slow is swallowed, both bump it by 40ms.  One dump
     per step - the dump is the wall clock, so we carry the reading forward.

  8. serial_for() takes the base port, so instance i maps to
     base_port + 2*i.  One emulator means base_port 5554; two means 5554 and
     5556.  Every caller passes it, so the signature must accept it.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

PKG = "com.discord"
LAUNCH_COMPONENTS = (
    f"{PKG}/.main.MainActivity",
    f"{PKG}/.main.MainDefault",
    f"{PKG}/.app.AppActivity",
)
SCREEN_W, SCREEN_H = 1080, 1920
ARTIFACTS = Path("artifacts")
DEFAULT_BASE_PORT = 5554

ANDROID_HOME = os.environ.get("ANDROID_HOME") or os.path.expanduser("~/Library/Android/sdk")
ADB = os.environ.get("ADB") or str(Path(ANDROID_HOME) / "platform-tools" / "adb")
EMULATOR = str(Path(ANDROID_HOME) / "emulator" / "emulator")

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_XP = re.compile(r"^//(?P<cls>[\w.*]+)(?:\[(?P<cond>.+)\])?$")

FOCUS_SOURCES = (
    "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'",
    "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'",
    "dumpsys window displays | grep mCurrentFocus",
    "dumpsys activity activities | grep -E 'mResumedActivity|ResumedActivity'",
)
SHADE_WINDOWS = ("NotificationShade", "StatusBar")
LOCK_WINDOWS = ("Keyguard", "Dream")

# picker geometry, read off the real modal: rows sit at y 895 / 1009 / 1123
WHEEL_CENTRE_Y = 1009
WHEEL_ROW_PITCH = 114            # one row == one gesture
WHEEL_CENTRE_BAND = (930, 1090)  # only the selected row is readable
WHEEL_COL_TOLERANCE = 80         # px - neighbouring columns are ~98 apart
WHEEL_FAST = 60                  # ms - starting drag duration
WHEEL_SLOW = 220                 # ms - cap
WHEEL_TUNE_STEP = 40             # ms - bump per correction
WHEEL_SETTLE = 0.08              # s - let the snap finish


class UiError(RuntimeError):
    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Node:
    cls: str = ""
    text: str = ""
    desc: str = ""
    res: str = ""
    bounds: tuple = (0, 0, 0, 0)
    clickable: bool = False

    @property
    def x(self): return self.bounds[0]

    @property
    def y(self): return self.bounds[1]

    @property
    def w(self): return max(0, self.bounds[2] - self.bounds[0])

    @property
    def h(self): return max(0, self.bounds[3] - self.bounds[1])

    @property
    def area(self): return self.w * self.h

    @property
    def cx(self): return (self.bounds[0] + self.bounds[2]) // 2

    @property
    def cy(self): return (self.bounds[1] + self.bounds[3]) // 2

    @property
    def label(self): return self.desc or self.text

    def contains(self, x, y):
        return self.bounds[0] <= x <= self.bounds[2] and self.bounds[1] <= y <= self.bounds[3]

    def __str__(self):
        bits = [self.cls or "?", f"centre=({self.cx}, {self.cy})"]
        if self.desc:
            bits.append(f"desc={self.desc!r}")
        if self.text:
            bits.append(f"text={self.text!r}")
        return "  ".join(bits)


def _attr(node: Node, attr: str) -> str:
    return {
        "text": node.text,
        "content-desc": node.desc,
        "resource-id": node.res,
        "class": node.cls,
    }.get(attr, "")


def _cond_match(node: Node, part: str) -> bool:
    part = part.strip()
    m = re.match(r"contains\(@([\w-]+),\s*'([^']*)'\)$", part)
    if m:
        return m.group(2) in _attr(node, m.group(1))
    m = re.match(r"@([\w-]+)\s*=\s*'([^']*)'$", part)
    if m:
        return _attr(node, m.group(1)) == m.group(2)
    return False


def xpath_match(node: Node, expr: str) -> bool:
    """Tiny xpath subset: //class[cond or cond] with @attr='v' / contains(@attr,'v')."""
    m = _XP.match(expr.strip())
    if not m:
        return False
    cls = m.group("cls")
    if cls != "*" and node.cls != cls:
        return False
    cond = m.group("cond")
    if not cond:
        return True
    return any(_cond_match(node, part) for part in cond.split(" or "))


def serial_for(instance: int, base_port: int = DEFAULT_BASE_PORT) -> str:
    """Instance i lives on base_port + 2*i: emulator-5554, emulator-5556, ..."""
    return f"emulator-{base_port + 2 * instance}"


def avd_for(serial: str):
    """emulator-5554 -> dcgen-0, emulator-5556 -> dcgen-1."""
    try:
        port = int(serial.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None
    return f"dcgen-{(port - DEFAULT_BASE_PORT) // 2}"


# ---------------------------------------------------------------------------
# device
# ---------------------------------------------------------------------------
class Device:
    def __init__(self, serial: str):
        self.serial = serial
        self.art = ARTIFACTS / serial
        self.art.mkdir(parents=True, exist_ok=True)
        self._wheel_tune = {}          # per-column drag duration

    # ---- adb plumbing ----------------------------------------------------
    def _popen(self, *args, timeout=60):
        return subprocess.run(
            [ADB, "-s", self.serial, *args], capture_output=True, timeout=timeout
        )

    def _adb(self, *args, timeout=60, check=True) -> str:
        p = self._popen(*args, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace")
        if check and p.returncode != 0:
            raise UiError("E_ADB", f"{' '.join(args)} rc={p.returncode} {p.stderr.decode()[:200]}")
        return out

    def shell(self, cmd: str, timeout=60, check=False) -> str:
        return self._adb("shell", cmd, timeout=timeout, check=check)

    def devices_listing(self) -> str:
        return subprocess.run([ADB, "devices", "-l"],
                              capture_output=True, timeout=20).stdout.decode("utf-8", "replace")

    def ensure_online(self, boot_timeout=180):
        """Raise E_DEVICE_OFFLINE unless this serial is attached AND booted."""
        listing = self.devices_listing()
        state = ""
        for line in listing.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial:
                state = parts[1]
        if state != "device":
            avd = avd_for(self.serial)
            hint = (f"{EMULATOR} -avd {avd} -no-snapshot-load &" if avd
                    else "start the emulator")
            raise UiError("E_DEVICE_OFFLINE",
                          f"{self.serial} state={state or 'absent'}\n"
                          f"  adb devices -l:\n{listing.strip()}\n"
                          f"  start it with: {hint}")

        deadline = time.time() + boot_timeout
        while time.time() < deadline:
            if self.shell("getprop sys.boot_completed").strip() == "1":
                return True
            time.sleep(2)
        raise UiError("E_DEVICE_OFFLINE", f"{self.serial} never finished booting")

    # ---- window state ----------------------------------------------------
    def current_focus(self) -> str:
        for cmd in FOCUS_SOURCES:
            out = self.shell(cmd).strip()
            if out:
                return out
        return ""

    def is_foreground(self) -> bool:
        return PKG in self.current_focus()

    def shade_open(self) -> bool:
        return any(w in self.current_focus() for w in SHADE_WINDOWS)

    def locked(self) -> bool:
        return any(w in self.current_focus() for w in LOCK_WINDOWS)

    def _shade_nudge(self, rung: int):
        if rung == 0:
            self.shell("cmd statusbar collapse", check=False)
        elif rung == 1:
            self.keyevent(4)                       # BACK
        elif rung == 2:
            self.swipe(540, 1860, 540, 1100, 250)  # drag the handle up
        else:
            self.keyevent(3)                       # HOME drops the shade outright

    def ensure_ready(self, tries=6) -> bool:
        """Clear the shade / keyguard. Both are separate windows that swallow
        taps and blank out state(). Returns False if it could not, so the caller
        can escalate (HOME + relaunch) instead of dying."""
        for rung in range(tries):
            focus = self.current_focus()
            if any(w in focus for w in SHADE_WINDOWS):
                self._shade_nudge(rung)
                time.sleep(1.0)
                continue
            if any(w in focus for w in LOCK_WINDOWS):
                self.shell("wm dismiss-keyguard", check=False)
                self.swipe(540, 1500, 540, 600, 200)
                time.sleep(1.0)
                continue
            return True
        return False

    # ---- dump ------------------------------------------------------------
    def raw_dump(self) -> str:
        return self._adb("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty", timeout=45)

    @staticmethod
    def _sanitise(raw: str) -> str:
        i, j = raw.find("<"), raw.rfind(">")
        if i < 0 or j <= i:
            raise UiError("E_UI_DUMP", "no xml in dump output")
        return raw[i:j + 1]

    def write_raw(self, raw: str, name="dump_raw.txt"):
        (self.art / name).write_text(raw, encoding="utf-8", errors="replace")

    def dump(self, retries=3):
        raw = ""
        last = None
        for _ in range(retries):
            raw = self.raw_dump()
            try:
                return ET.fromstring(self._sanitise(raw))
            except Exception as exc:          # truncated dump: usually a live animation
                last = exc
                self.shell("settings put global animator_duration_scale 0")
                self.shell("settings put global window_animation_scale 0")
                self.shell("settings put global transition_animation_scale 0")
                time.sleep(0.6)
        self.write_raw(raw)
        raise UiError("E_UI_DUMP", f"unparseable dump after {retries} tries: {last}")

    def nodes(self, root=None):
        root = self.dump() if root is None else root
        out = []
        for el in root.iter("node"):
            m = _BOUNDS.match(el.get("bounds", ""))
            if not m:
                continue
            out.append(Node(
                cls=el.get("class", ""),
                text=el.get("text", ""),
                desc=el.get("content-desc", ""),
                res=el.get("resource-id", ""),
                bounds=tuple(int(g) for g in m.groups()),
                clickable=el.get("clickable") == "true",
            ))
        return out

    def snapshot(self, name: str, root=None):
        root = self.dump() if root is None else root
        raw = ET.tostring(root, encoding="unicode")
        (self.art / f"{name}.xml").write_text(raw, encoding="utf-8")
        png = self._popen("exec-out", "screencap", "-p", timeout=45).stdout
        (self.art / f"{name}.png").write_bytes(png)
        return [n for n in self.nodes(root) if n.text or n.desc]

    def capture_png(self) -> bytes:
        """Raw screencap bytes, for OCR and vision calls."""
        return self._popen("exec-out", "screencap", "-p", timeout=45).stdout

    # ---- input -----------------------------------------------------------
    def tap(self, x, y):
        self._adb("shell", "input", "tap", str(int(x)), str(int(y)))

    def swipe(self, x1, y1, x2, y2, duration=200):
        self._adb("shell", "input", "swipe", str(int(x1)), str(int(y1)),
                  str(int(x2)), str(int(y2)), str(int(duration)))

    def keyevent(self, code):
        self._adb("shell", "input", "keyevent", str(code))

    def input_text(self, text: str):
        # `input text` decodes %s as a space; nothing else needs url-decoding.
        self._adb("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))

    # ---- app lifecycle ---------------------------------------------------
    def pm_clear(self):
        self.shell(f"pm clear {PKG}")

    def _launch_loop(self):
        for comp in LAUNCH_COMPONENTS:
            self._adb("shell", "am", "start", "-W", "-n", comp, check=False)
            time.sleep(1.2)
            if self.is_foreground():
                return comp
        self.shell(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")
        time.sleep(2.5)
        return "monkey" if self.is_foreground() else None

    def start_app(self, force_stop=True) -> str:
        self.ensure_online()
        self.ensure_ready()
        if force_stop:
            self.shell(f"am force-stop {PKG}")

        launched = self._launch_loop()
        if launched:
            self.ensure_ready()
            return launched

        self.keyevent(3)
        time.sleep(1.0)
        self.ensure_ready()
        launched = self._launch_loop()
        if launched:
            self.ensure_ready()
            return launched

        raise UiError("E_APP_NOT_FOREGROUND",
                      f"focus={self.current_focus()!r} "
                      f"installed={self.shell('pm list packages ' + PKG).strip()!r}")

    # ---- matching --------------------------------------------------------
    def _match(self, nodes, kind, value):
        if kind == "res":
            return [n for n in nodes if n.res == value or n.res.endswith("/" + value)]
        if kind == "text":
            return [n for n in nodes if n.text == value]
        if kind == "text_contains":
            return [n for n in nodes if value in n.text]
        if kind == "desc":
            return [n for n in nodes if n.desc == value]
        if kind == "desc_contains":
            return [n for n in nodes if value in n.desc]
        if kind == "label":                 # R1: text OR content-desc, exact
            return [n for n in nodes if n.text == value or n.desc == value]
        if kind == "label_contains":
            return [n for n in nodes if value in n.text or value in n.desc]
        if kind == "class":
            return [n for n in nodes if n.cls == value]
        if kind == "xpath":
            return [n for n in nodes if xpath_match(n, value)]
        if kind == "coord":
            x, y = value
            hits = [n for n in nodes if n.area and n.contains(x, y)]
            if hits:                        # smallest containing node
                return [min(hits, key=lambda n: n.area)]
            return [Node(cls="coord", bounds=(x, y, x, y))]
        if kind == "ocr":
            return self._ocr(value)
        return []

    def resolve(self, spec, nodes=None, allow_coord=True, strict_coord=False):
        nodes = self.nodes() if nodes is None else nodes
        for s in spec.get("strategies", []):
            kind = s["kind"]
            if kind == "coord" and not allow_coord:
                continue
            hits = self._match(nodes, kind, s["value"])
            if not hits:
                continue
            if kind == "coord" and strict_coord and hits[0].cls == "coord":
                continue                    # empty space is not a hit
            hits.sort(key=lambda n: (n.y, n.x))
            if "nth" in s:
                idx = s["nth"]
                if idx >= len(hits):
                    continue
                return hits[idx], kind
            # prefer the clickable, then the smallest - the label node is often
            # a non-clickable TextView sibling of the real Button
            return min(hits, key=lambda n: (not n.clickable, n.area)), kind
        return None, None

    def resolve_name(self, sel, name, nodes=None, allow_coord=True, strict_coord=False):
        return self.resolve(sel["elements"][name], nodes, allow_coord, strict_coord)

    def present(self, sel, name, nodes=None):
        """STRICT presence check. Never trust a non-strict one for this."""
        return bool(self.resolve_name(sel, name, nodes, strict_coord=True)[0])

    def tap_el(self, sel, name, retries=6, strict_coord=False):
        for _ in range(retries):
            node, kind = self.resolve_name(sel, name, strict_coord=strict_coord)
            if node:
                self.tap(node.cx, node.cy)
                return node, kind
            time.sleep(0.4)
        raise UiError("E_UI_ELEMENT_MISSING", f"{name} not on screen")

    def tap_label(self, label, retries=8):
        """Re-dump on EVERY attempt: the tree at tap time differs from the tree
        that was printed."""
        last = []
        for _ in range(retries):
            nodes = self.nodes()
            for kind in ("label", "label_contains"):
                hits = self._match(nodes, kind, label)
                if hits:
                    n = min(hits, key=lambda n: (not n.clickable, n.area))
                    self.tap(n.cx, n.cy)
                    return n
            last = [n for n in nodes if n.clickable][:6]
            time.sleep(0.4)
        raise UiError("E_UI_ELEMENT_MISSING",
                      f"nothing labelled {label!r}; clickables={[str(n) for n in last]}")

    # ---- state -----------------------------------------------------------
    def state(self, sel, nodes=None):
        nodes = self.nodes() if nodes is None else nodes
        matched = []
        for name, spec in sel.get("states", {}).items():
            det = spec.get("detect", [])
            if not det:
                continue
            hits = sum(
                1 for el in det
                if self.resolve_name(sel, el, nodes, allow_coord=False)[0]
            )
            need = len(det) if spec.get("require_all") else 1
            if hits >= need:
                matched.append((spec.get("priority", 50), name))
        matched.sort(key=lambda t: -t[0])
        return [n for _, n in matched]

    def wait_state(self, sel, names, timeout=20, poll=0.6):
        """Returns the first name from `names` that is present, regardless of
        where it sits in the priority order."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.state(sel)
            for n in names:
                if n in st:
                    return n
            time.sleep(poll)
        return None

    def wait_gone(self, sel, name, timeout=10, poll=0.4):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.present(sel, name):
                return True
            time.sleep(poll)
        return False

    def expect(self, sel, names, timeout=15, what=""):
        got = self.wait_state(sel, names, timeout)
        if got is None:
            raise UiError("E_STATE_MISMATCH",
                          f"{what or 'expected'} {names}, saw {self.state(sel)}")
        return got

    # ---- wheel -----------------------------------------------------------
    def read_wheel(self, x, band=WHEEL_CENTRE_BAND):
        """Read the CENTRE ROW of the picker column at x.

        Band-limited: only y within `band` counts, so a column can never latch
        onto a row above or below the selected one. Falls back to any node in
        the band if no EditText is present.
        """
        fallback = None
        for n in self.nodes():
            if abs(n.cx - x) > WHEEL_COL_TOLERANCE:
                continue
            if not (band[0] <= n.cy <= band[1]):
                continue
            if n.cls == "android.widget.EditText":
                return n.text.strip()
            if fallback is None and n.text:
                fallback = n.text.strip()
        return fallback

    @staticmethod
    def _rank(v):
        if v is None:
            return 0
        if v.isdigit():
            return int(v)
        months = ["January", "February", "March", "April", "May", "June", "July",
                  "August", "September", "October", "November", "December"]
        return months.index(v) if v in months else 0

    def wheel_step(self, x, up, duration, pitch=WHEEL_ROW_PITCH):
        """Exactly ONE row per gesture.

        The drag spans one row pitch, centred on the selected row, so it starts
        and ends inside the picker's touch area (y 838..1180) AND the release
        lands on the adjacent snap point rather than three rows away.
        """
        half = pitch // 2
        y1 = WHEEL_CENTRE_Y + half if up else WHEEL_CENTRE_Y - half
        y2 = WHEEL_CENTRE_Y - half if up else WHEEL_CENTRE_Y + half
        self.swipe(x, y1, x, y2, duration=duration)

    def _nudge_row(self, x, cur, target):
        """Instant fallback: tap the row one step toward the target."""
        rows = {n.y: n for n in self.nodes()
                if n.cls == "android.widget.Button" and abs(n.cx - x) < WHEEL_COL_TOLERANCE}
        if not rows:
            return
        ys = sorted(rows)
        pick = rows[ys[-1]] if self._rank(target) > self._rank(cur) else rows[ys[0]]
        self.tap(pick.cx, pick.cy)

    def wheel(self, x, target, max_steps=120):
        """Advance the column at x one row per gesture, as fast as it will go.

        One dump per step - the dump dominates the wall clock, so the previous
        reading is carried forward instead of re-read at the top of the loop.
        Drag duration is tuned per column: too fast and the fling carries extra
        rows, too slow and the drag is swallowed. Both bump it by 40ms until a
        gesture moves exactly one row; two swallowed drags fall back to a tap.

        Raises E_DOB_WHEEL_GONE if the column is unreadable for 8 polls, which
        is what a CLOSED modal looks like.
        """
        duration = self._wheel_tune.get(x, WHEEL_FAST)
        cur = self.read_wheel(x)
        blind = 0
        swallowed = 0

        for step in range(max_steps):
            if cur == target:
                return step
            if cur is None:
                blind += 1
                if blind >= 8:
                    raise UiError("E_DOB_WHEEL_GONE", f"no readable row at x={x}")
                time.sleep(0.2)
                cur = self.read_wheel(x)
                continue
            blind = 0

            up = self._rank(target) > self._rank(cur)
            before = self._rank(cur)
            self.wheel_step(x, up, duration)
            time.sleep(WHEEL_SETTLE)
            prev, cur = cur, self.read_wheel(x)

            if cur is None:
                continue
            moved = abs(self._rank(cur) - before)
            if moved == 0:
                swallowed += 1
                duration = min(WHEEL_SLOW, duration + WHEEL_TUNE_STEP)
                self._wheel_tune[x] = duration
                if swallowed >= 2:
                    self._nudge_row(x, prev, target)
                    time.sleep(WHEEL_SETTLE)
                    cur = self.read_wheel(x)
                    swallowed = 0
            elif moved > 1:
                # fling carried extra rows - slower release settles on one
                duration = min(WHEEL_SLOW, duration + WHEEL_TUNE_STEP)
                self._wheel_tune[x] = duration
            else:
                swallowed = 0

        raise UiError("E_DOB_FAIL", f"wheel x={x} stuck on {cur!r} (wanted {target!r})")

    # ---- ocr (last resort) ----------------------------------------------
    def _ocr(self, needle):
        if not shutil.which("tesseract"):
            return []
        png = self.capture_png()
        t = subprocess.run(["tesseract", "-", "stdout", "--psm", "11", "tsv"],
                           input=png, capture_output=True)
        words = []
        for row in t.stdout.decode("utf-8", "replace").splitlines()[1:]:
            c = row.split("\t")
            if len(c) < 12 or not c[11].strip():
                continue
            words.append((c[11], int(c[6]), int(c[7]), int(c[8]), int(c[9])))
        parts = needle.split()
        for i in range(len(words) - len(parts) + 1):
            chunk = words[i:i + len(parts)]
            if [w[0] for w in chunk] == parts:
                x = sum(w[1] + w[3] // 2 for w in chunk) // len(parts)
                y = sum(w[2] + w[4] // 2 for w in chunk) // len(parts)
                return [Node(cls="ocr", text=needle, bounds=(x, y, x, y))]
        return []


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def load_selectors(path=None):
    if path is None:
        for cand in ("configs/selectors.yaml", "config/selectors.yaml", "selectors.yaml"):
            if Path(cand).exists():
                path = cand
                break
        else:
            raise UiError("E_CONFIG", "selectors.yaml not found")
    try:
        import yaml
    except ImportError:
        raise UiError("E_CONFIG", "PyYAML missing - .venv/bin/pip install pyyaml")
    with open(path) as fh:
        return yaml.safe_load(fh)