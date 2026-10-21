"""Low-level device handle: adb, uiautomator dumps, screenshots, app control.

Resolution always runs against a real uiautomator XML dump, so behaviour is
identical whether or not the uiautomator2 helper is usable. uiautomator2 is only
used for text entry and gestures, where `adb shell input` is lossy.

DUMP ROBUSTNESS - read this before changing `dump()`:
`uiautomator dump` prefixes its output with "UI hierchary dumped to: /dev/tty"
and Discord's React Native tree emits no `<?xml` declaration. Slicing on
`find("<?xml")` therefore does nothing and ET.fromstring dies with
"syntax error: line 1, column 0". It ALSO appends the same message after
</hierarchy> on some builds. `_sanitise` slices on the outermost angle brackets
instead. The CLI also refuses to dump while the app animates, so `dump()` retries.

APP LAUNCH - read this before changing `start_app()`:
`am start -n <pkg>/<activity>` fails silently if the activity name is wrong, and
the flow then happily operates on the Android home screen (whose grid cells
satisfy every coord fallback). `start_app` therefore tries several component
names, verifies the package actually owns the foreground, and raises.
"""
from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.errors import DCGenError, FailureCode
from core.log import log
from core.token import TOKEN_RE

DUMP_TTL = 1.5
FOREGROUND_TTL = 1.0
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _escape_input(text: str) -> str:
    """`adb shell input text` needs spaces as %s and single quotes escaped.

    It does NOT url-decode: a gmail-plus address must use a literal '+'.
    """
    return "'" + text.replace(" ", "%s").replace("'", "'\\''") + "'"


@dataclass
class Found:
    by: str
    value: object
    x: int
    y: int
    node: object
    #: False when the hit came from a coord/ocr fallback rather than the tree.
    #: exists() is deliberately non-strict, so this is informational there;
    #: exists_strict() and wait_gone() use it to reject fallbacks.
    confident: bool = True

    def __str__(self) -> str:
        return f"{self.by}={self.value!r} at ({self.x}, {self.y})" + (
            "" if self.confident else " [fallback]")


class Device:
    #: tried in order; the real one is whatever `cmd package resolve-activity`
    #: reports for this APK build
    APP_ACTIVITIES = (".main.MainActivity", ".main.MainDefault", ".app.AppActivity")

    def __init__(self, backend, idx: int, cfg):
        self.backend = backend
        self.idx = idx
        self.cfg = cfg
        self.serial = backend.adb_serial(idx)
        self._u2 = None
        self._u2_tried = False
        self._dump_cache: tuple[float, ET.Element] | None = None
        self._fg_cache: tuple[float, str] | None = None
        self._last_raw: str | None = None
        self._size: tuple[int, int] | None = None

    # ---- adb -------------------------------------------------------------
    def adb(self, *args: str, timeout: int = 30, check: bool = True,
            binary: bool = False):
        return self.backend.adb_run(self.idx, *args, timeout=timeout,
                                    check=check, binary=binary)

    def shell(self, cmd: str, timeout: int = 30, check: bool = True) -> str:
        return self.adb("shell", cmd, timeout=timeout, check=check)

    def root(self) -> bool:
        out = self.adb("root", check=False, timeout=15)
        return "cannot run as root" not in out

    # ---- screen ----------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        if self._size:
            return self._size
        out = self.shell("wm size", check=False)
        m = re.search(r"Override size:\s*(\d+)x(\d+)", out) or \
            re.search(r"Physical size:\s*(\d+)x(\d+)", out)
        self._size = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
        return self._size

    def screenshot_png(self) -> bytes:
        return self.adb("exec-out", "screencap", "-p", binary=True, timeout=30)

    # ---- hierarchy -------------------------------------------------------
    def _dump_once(self) -> str:
        """One raw attempt, unvalidated. Prefers /dev/tty (single round trip)."""
        try:
            out = self.adb("exec-out", "uiautomator", "dump", "--compressed",
                           "/dev/tty", timeout=45, check=False)
            if "<hierarchy" in out:
                return out
        except DCGenError:
            pass
        # some builds only honour a real path
        self.shell("rm -f /sdcard/window_dump.xml", check=False)
        self.shell("uiautomator dump --compressed /sdcard/window_dump.xml",
                   check=False)
        return self.shell("cat /sdcard/window_dump.xml", check=False)

    @staticmethod
    def _sanitise(raw: str) -> str:
        """Slice to the outermost angle brackets. Kills the 'UI hierchary dumped
        to:' prefix AND the same message appended after </hierarchy>."""
        start = raw.find("<")
        if start < 0:
            return ""
        end = raw.rfind(">")
        return raw[start:end + 1] if end > start else raw[start:]

    def dump(self, cached: bool = True) -> ET.Element:
        if cached and self._dump_cache and time.time() - self._dump_cache[0] < DUMP_TTL:
            return self._dump_cache[1]
        last = "no output"
        for attempt in range(3):
            raw = self._dump_once()
            self._last_raw = raw
            xml = self._sanitise(raw)
            if xml:
                try:
                    root = ET.fromstring(xml)
                    self._dump_cache = (time.time(), root)
                    return root
                except ET.ParseError as exc:
                    last = str(exc)
            else:
                last = "empty dump"
            # RN never idles, so uiautomator gives up mid-animation. Nudge it.
            for key in ("window_animation_scale", "transition_animation_scale",
                        "animator_duration_scale"):
                self.shell(f"settings put global {key} 0", check=False)
            time.sleep(0.7)
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                         f"unparseable dump after 3 tries: {last}")

    def invalidate(self) -> None:
        self._dump_cache = None

    @property
    def dump_is_fresh(self) -> bool:
        return bool(self._dump_cache and time.time() - self._dump_cache[0] < DUMP_TTL)

    @property
    def dump_generation(self) -> float:
        """Stamp of the cached dump, for one-screencap-per-dump."""
        return self._dump_cache[0] if self._dump_cache else 0.0

    @staticmethod
    def nodes(root: ET.Element) -> list:
        return list(root.iter("node"))

    @staticmethod
    def bounds_of(node) -> tuple[int, int, int, int] | None:
        m = BOUNDS_RE.match(node.get("bounds") or "")
        if not m:
            return None
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    @staticmethod
    def visible(node) -> bool:
        return Device.bounds_of(node) is not None

    @staticmethod
    def label(node) -> str:
        """R1: a Button's label lives in content-desc, not text."""
        return (node.get("content-desc") or "") or (node.get("text") or "")

    @staticmethod
    def centre_of(node) -> tuple[int, int] | None:
        b = Device.bounds_of(node)
        if not b:
            return None
        x1, y1, x2, y2 = b
        return (x1 + x2) // 2, (y1 + y2) // 2

    # ---- input -----------------------------------------------------------
    def tap_xy(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", check=False)
        self.invalidate()
        time.sleep(0.25)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}", check=False)
        self.invalidate()
        time.sleep(0.2)

    def keyevent(self, code: int) -> None:
        """KEYCODE_*. 4 = BACK, 66 = ENTER, 67 = DEL - none of these navigate
        when there is no keyboard up, which is why the flow never uses ESC."""
        self.shell(f"input keyevent {int(code)}", check=False)
        self.invalidate()
        time.sleep(0.15)

    def type_text(self, value: str) -> None:
        d = self.u2
        if d is not None:
            try:
                d.send_keys(value)
                self.invalidate()
                return
            except Exception:
                pass
        self.shell("input text " + _escape_input(value), check=False)
        self.invalidate()
        time.sleep(0.2)

    def enter(self) -> None:
        self.keyevent(66)
        time.sleep(0.3)

    def hide_keyboard(self) -> None:
        # ESC dismisses the IME without navigating back
        self.shell("input keyevent 111", check=False)
        time.sleep(0.3)

    # ---- uiautomator2 (optional) -----------------------------------------
    @property
    def u2(self):
        if self._u2 is None and not self._u2_tried:
            self._u2_tried = True
            try:
                import uiautomator2 as u2
                self._u2 = u2.connect(self.serial)
                log("uiautomator2 connected")
            except Exception as exc:
                log(f"uiautomator2 unavailable ({exc}) - adb input only", "warn")
                self._u2 = None
        return self._u2

    # ---- app control -----------------------------------------------------
    def foreground(self, ttl: float = FOREGROUND_TTL) -> str:
        if self._fg_cache and time.time() - self._fg_cache[0] < ttl:
            return self._fg_cache[1]
        focus = ""
        for cmd in ("dumpsys window | grep -m1 mCurrentFocus",
                    "dumpsys activity activities | grep -m1 mResumedActivity"):
            out = self.shell(cmd, check=False)
            if out.strip():
                focus = out.strip()
                break
        self._fg_cache = (time.time(), focus)
        return focus

    def start_app(self) -> None:
        """Launch and VERIFY. A silent am start leaves us on the home screen,
        whose grid cells satisfy every coord fallback - a very confusing bug."""
        self.shell("input keyevent 224", check=False)      # wake
        self.shell("wm dismiss-keyguard", check=False)
        pkg = self.cfg.app.package
        last = ""
        for act in self.APP_ACTIVITIES:
            last = self.adb("shell", "am", "start", "-W", "-n", f"{pkg}/{act}",
                            check=False, timeout=30)
            time.sleep(2)
            self._fg_cache = None
            if pkg.split(".")[-1] in self.foreground().lower():
                log(f"launched {pkg}/{act}")
                self.invalidate()
                time.sleep(2)
                return
        self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
                   check=False)
        time.sleep(3)
        self._fg_cache = None
        fg = self.foreground()
        if pkg.split(".")[-1] not in fg.lower():
            raise DCGenError(
                FailureCode.E_UNKNOWN,
                f"{pkg} never reached the foreground (focus={fg!r}); "
                f"last am start said: {last.strip()[:200]}")
        log(f"launched {pkg} via monkey")
        self.invalidate()
        time.sleep(2)

    def stop_app(self) -> None:
        self.shell(f"am force-stop {self.cfg.app.package}", check=False)
        self.invalidate()

    def clear_app(self) -> None:
        self.shell(f"pm clear {self.cfg.app.package}", check=False)
        self.invalidate()

    def open_url(self, url: str, package: str | None = None) -> None:
        """`package` pins the handler when several browsers are installed;
        email_verify passes None and lets Android choose."""
        cmd = f"am start -a android.intent.action.VIEW -d '{url}'"
        if package:
            cmd += f" -p {package}"
        self.shell(cmd, check=False)
        self.invalidate()

    # ---- token in shared prefs -------------------------------------------
    def prefs_token(self) -> str | None:
        if not self.root():
            return None
        pkg = self.cfg.app.package
        path = f"/data/data/{pkg}/shared_prefs/{pkg}_preferences.xml"
        out = self.shell(f"cat {path}", check=False)
        m = TOKEN_RE.search(out)
        return m.group(0) if m else None

    def inject_token(self, token: str) -> bool:
        if not self.root():
            return False
        pkg = self.cfg.app.package
        path = f"/data/data/{pkg}/shared_prefs/{pkg}_preferences.xml"
        uid = self.shell(f"stat -c %u /data/data/{pkg}", check=False).strip()
        if not uid.isdigit():
            return False
        self.stop_app()
        if self.shell(f"test -f {path} && echo yes", check=False).strip() != "yes":
            return False
        self.shell(f"sed -i 's#<string name=\"token\">[^<]*</string>"
                   f"#<string name=\"token\">{token}</string>#' {path}", check=False)
        if token not in self.shell(f"cat {path}", check=False):
            return False
        self.shell(f"chown {uid}:{uid} {path}", check=False)
        self.shell(f"chmod 660 {path}", check=False)
        return True

    # ---- evidence --------------------------------------------------------
    def capture_failure(self, state: str, note: str = "") -> Path:
        if not self.cfg.run.capture_on_fail:
            return Path()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = self.cfg.paths.shots / f"{stamp}_{self.idx}_{state}"
        out.mkdir(parents=True, exist_ok=True)
        try:
            (out / "screen.png").write_bytes(self.screenshot_png())
        except Exception:
            pass
        # always keep the raw bytes, so a parse failure stays diagnosable
        try:
            raw = self._last_raw
            if raw is None:
                raw = self._dump_once()
            (out / "dump_raw.txt").write_text(raw or "")
        except Exception:
            pass
        try:
            (out / "hierarchy.xml").write_text(
                ET.tostring(self.dump(cached=False), encoding="unicode"))
        except Exception:
            pass
        if note:
            (out / "note.txt").write_text(note)
        log(f"evidence -> {out}", "warn")
        return out