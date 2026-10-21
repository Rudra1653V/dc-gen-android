"""Discord signup flow, driven from an Account record.

NO keyevent 111 (ESC) anywhere.  ESC is not a dedicated IME key: with no
keyboard up it is handled as BACK and silently unwinds the whole signup.

CAPTCHA IS HUMAN.  step_captcha reaches the native wrapper, beeps, and then
polls until the wrapper is gone - so you solve it in the AVD window and the tool
carries on by itself. Set [captcha] mode = "auto" to use core/captcha.py instead.

R6: the identify screen comes up in PHONE mode - tap Email AND blur.
R7: the create-account username is NOT reliably pre-filled; we supply our own
    randomised one and retry with a fresh candidate if it is taken.
R8: the form's single Clear button belongs to the username - scope it by y.
R9: the notification shade swallows every tap - ensure_ready() first.
"""
from __future__ import annotations

import random
import re
import time

from core.names import USERNAME_WORDS
from core.ui import UiError, load_selectors

DOB_WHEELS = {"year": 682, "month": 441, "day": 584}
CAPTCHA_IFRAME_SKIP = (784, 1247)
BLUR_POINT = (540, 278)
CLEAR_TOLERANCE = 80

FIELD_STATE = {
    "email_field": "identify",
    "display_name_field": "register_name",
    "username_field": "register_form",
    "password_field": "register_form",
}


def username_candidate(prefix="", digits=6, sep="", max_len=20):
    base = prefix + random.choice(USERNAME_WORDS)
    suffix = "".join(random.choice("0123456789") for _ in range(digits))
    cand = re.sub(r"[^a-z0-9._]", "", f"{base}{sep}{suffix}".lower())
    return cand[:max_len]


class Signup:
    def __init__(self, dev, sel, account, log=print, cfg=None):
        self.dev = dev
        self.sel = sel
        self.acct = account
        self.log = log
        self.cfg = cfg
        self.username = account.username

    # convenience
    @property
    def email(self): return self.acct.email

    @property
    def password(self): return self.acct.password

    @property
    def name(self): return self.acct.display_name

    @property
    def dob(self): return self.acct.dob

    # ---- helpers ---------------------------------------------------------
    def _retries(self):
        return getattr(self.cfg.flow, "retries", 2) if self.cfg else 2

    def _submit_timeout(self):
        return getattr(self.cfg.flow, "submit_timeout_s", 45) if self.cfg else 45

    def _captcha_wait(self):
        return getattr(self.cfg.flow, "captcha_wait_s", 25) if self.cfg else 25

    def hide_keyboard(self):
        out = self.dev.shell("dumpsys input_method | grep -m1 mInputShown")
        if "mInputShown=true" in out:
            self.dev.keyevent(4)
            time.sleep(0.5)

    def _blur(self):
        self.dev.tap(*BLUR_POINT)
        time.sleep(0.5)

    def _field_text(self, el_name):
        node, _ = self.dev.resolve_name(self.sel, el_name, strict_coord=True)
        return node.text if node else None

    def _clear_field(self, node, el_name, value):
        clear, _ = self.dev.resolve_name(self.sel, "clear_button")
        if clear and abs(clear.cy - node.cy) <= CLEAR_TOLERANCE:
            self.dev.tap(clear.cx, clear.cy)
            time.sleep(0.3)
            return
        for _ in range(len(value) + 4):
            self.dev.keyevent(67)

    def _fill(self, el_name, value, verify=True, masked=False, soft=False, tries=2):
        want = FIELD_STATE.get(el_name)
        for attempt in range(1, tries + 1):
            if want:
                self.dev.expect(self.sel, [want], 15, f"field {el_name} needs")
            node, _ = self.dev.resolve_name(self.sel, el_name, strict_coord=True)
            if not node:
                raise UiError("E_UI_ELEMENT_MISSING",
                              f"{el_name} not on screen (state={self.dev.state(self.sel)})")
            self.dev.tap(node.cx, node.cy)
            time.sleep(0.5)
            self._clear_field(node, el_name, value)
            self.dev.input_text(value)
            time.sleep(0.5)
            if not verify:
                return
            got = self._field_text(el_name)
            if got is None:
                raise UiError("E_UI_ELEMENT_MISSING", f"{el_name} vanished after typing")
            if masked:
                bad = len(got) != len(value)
                detail = f"holds {len(got)} chars, wanted {len(value)}"
            else:
                bad = got != value
                detail = f"reads {got!r}, wanted {value!r}"
            if not bad:
                self.log(f"   filled {el_name} = {got!r}")
                return
            if attempt < tries:
                self.log(f"   !! {el_name} {detail} - retrying")
                time.sleep(0.5)
                continue
            if soft:
                self.log(f"   !! {el_name} {detail}")
            else:
                raise UiError("E_UI_FIELD", f"{el_name} {detail}")

    def _phone_mode(self):
        return self.dev.present(self.sel, "country_code")

    def _use_email_tab(self, timeout=6):
        if not self._phone_mode():
            return True
        for attempt in (1, 2):
            self.dev.tap_el(self.sel, "email_tab")
            if self.dev.wait_gone(self.sel, "country_code", timeout):
                self.log("   email tab engaged")
                return True
            self.log(f"   email tab tap {attempt} did not take")
        self.log("   !! still phone mode - relying on blur to force the switch")
        return False

    def _await_email_mode(self, timeout=8):
        if self.dev.wait_gone(self.sel, "country_code", timeout):
            return True
        self.log("   !! field never left phone mode")
        return False

    def _recover_to(self, want):
        if want in self.dev.state(self.sel):
            return True
        st = self.dev.state(self.sel)
        self.log(f"   !! on {st}, want {want}")
        if "welcome" in st:
            self.step_welcome()
            return want in self.dev.state(self.sel)
        return False

    def _log_clickables(self):
        for n in [x for x in self.dev.nodes() if x.clickable][:10]:
            self.log(f"      clickable: {n}")

    def _tap_until(self, element, expect_state, retries=None, timeout=12, focus=None):
        retries = retries or self._retries() + 1
        for attempt in range(retries):
            if not self.dev.ensure_ready():
                self.log(f"   !! overlay stuck: {self.dev.current_focus()!r}")
            self.hide_keyboard()
            if attempt == 2:
                if focus:
                    n, _ = self.dev.resolve_name(self.sel, focus, strict_coord=True)
                    if n:
                        self.log(f"   re-focusing {focus}")
                        self.dev.tap(n.cx, n.cy)
                        time.sleep(0.3)
                self.log("   trying ENTER")
                self.dev.keyevent(66)
            node, _ = self.dev.resolve_name(self.sel, element, strict_coord=True)
            if node is None:
                self.log(f"   no {element} on screen")
            elif attempt == 1:
                self.log(f"   long-press {element} at ({node.cx}, {node.cy})")
                self.dev.swipe(node.cx, node.cy, node.cx, node.cy, 150)
            else:
                self.log(f"   tap {element} at ({node.cx}, {node.cy}) via {node.cls}")
                self.dev.tap(node.cx, node.cy)

            got = self.dev.wait_state(self.sel, [expect_state, "welcome", "error_modal"], timeout)
            if got == expect_state:
                time.sleep(0.4)
                return got
            if got == "error_modal":
                return got
            self.log(f"   {element} attempt {attempt + 1}/{retries} did not advance "
                     f"(state={self.dev.state(self.sel)})")
            if attempt == 0 and focus:
                n, _ = self.dev.resolve_name(self.sel, focus, strict_coord=True)
                if n:
                    self.log(f"   re-focusing {focus}")
                    self.dev.tap(n.cx, n.cy)
            if attempt == retries - 1:
                self._log_clickables()
            time.sleep(0.8)
        raise UiError("E_STATE_MISMATCH",
                      f"{expect_state} never reached; state={self.dev.state(self.sel)}")

    def _await_submit_outcome(self):
        window = self._submit_timeout()
        for r in range(3):
            deadline = time.time() + window
            while time.time() < deadline:
                st = self.dev.state(self.sel)
                for name in ("human_check", "captcha_wrapper", "captcha_iframe",
                             "pending_verify", "logged_in", "dob_error_modal"):
                    if name in st:
                        return name
                time.sleep(0.8)
            self.log(f"   submit still pending, round {r + 1}/3")
        raise UiError("E_SUBMIT_TIMEOUT", f"no post-submit screen after 3 x {window}s")

    # ---- steps -----------------------------------------------------------
    def step_welcome(self):
        return self._tap_until("register_button", "identify", 2, 20)

    def step_identify(self, tries=3):
        last = None
        for attempt in range(1, tries + 1):
            st = self.dev.state(self.sel)
            if "welcome" in st or "identify" not in st:
                self.log(f"   identify attempt {attempt}: state={st}")
                if "welcome" in st:
                    self.step_welcome()
                else:
                    self._recover_to("identify")
            self._use_email_tab()
            try:
                self._fill("email_field", self.email, tries=1)
            except UiError as exc:
                last = exc
                self.log(f"   identify attempt {attempt} failed: {exc}")
                time.sleep(0.6)
                continue
            self._blur()
            self._await_email_mode()
            if "identify" in self.dev.state(self.sel):
                return "identify"
            last = UiError("E_UI_FIELD", f"identify lost, state={self.dev.state(self.sel)}")
            time.sleep(0.6)
        raise last or UiError("E_UI_FIELD", "identify never stuck")

    def step_email_next(self, retries=2):
        for attempt in range(retries + 1):
            self.dev.ensure_ready()
            if not self._recover_to("identify"):
                raise UiError("E_UI_ELEMENT_MISSING",
                              f"not on identify, state={self.dev.state(self.sel)}")
            node = self.dev.tap_el(self.sel, "email_next_button")[0]
            self.log(f"   tap 'Next' at ({node.cx}, {node.cy}) via {node.cls}")
            got = self.dev.wait_state(self.sel, ["register_name", "phone_error"], 12)
            if got == "register_name":
                time.sleep(0.4)
                return "register_name"
            if got == "phone_error":
                self.log(f"   !! validated in phone mode (attempt {attempt + 1}) - retrying")
                self._use_email_tab()
                self._fill("email_field", self.email)
                self._blur()
                self._await_email_mode()
                continue
            raise UiError("E_UI_ELEMENT_MISSING",
                          f"Next did nothing, state={self.dev.state(self.sel)}")
        raise UiError("E_EMAIL_INVALID", "Next never advanced past identify")

    def step_name(self):
        self._fill("display_name_field", self.name, soft=True)
        self._blur()
        return self._tap_until("name_next_button", "register_form", 3, 12,
                               focus="display_name_field")

    def _ensure_username(self, tries=4):
        """Use the account's randomised username; retry with a fresh candidate
        only if Discord rejects it as taken."""
        node, _ = self.dev.resolve_name(self.sel, "username_field", strict_coord=True)
        if node and node.text.strip() and not self.dev.present(self.sel, "username_hint"):
            self.username = node.text.strip()
            self.log(f"   username pre-filled = {self.username!r}")
            return self.username

        for attempt in range(1, tries + 1):
            candidate = self.username if attempt == 1 else username_candidate()
            self.log(f"   typing username {candidate!r}")
            self._fill("username_field", candidate, soft=True)
            self._blur()
            if self.dev.resolve_name(self.sel, "username_ok")[0]:
                self.username = self._field_text("username_field") or candidate
                self.log(f"   username accepted = {self.username!r}")
                self.acct.username = self.username
                return self.username
            if self.dev.resolve_name(self.sel, "username_bad")[0]:
                self.log(f"   {candidate!r} rejected - retrying")
                continue
            self.log(f"   no verdict on {candidate!r} (attempt {attempt}/{tries})")
        self.username = self._field_text("username_field")
        self.acct.username = self.username
        return self.username

    def step_form_fill(self):
        self.dev.expect(self.sel, ["register_form"], 12, "form")
        self._ensure_username()
        self._fill("password_field", self.password, masked=True)
        self.hide_keyboard()
        time.sleep(0.5)
        got = self._field_text("password_field")
        if got is None or len(got) != len(self.password):
            self.log(f"   !! password reads {got!r} after blur - refilling")
            self._fill("password_field", self.password, masked=True)
            self.hide_keyboard()
            time.sleep(0.5)
        return "register_form"

    def step_form_next(self):
        return self._tap_until("form_next_button", "register_dob", 3, 15,
                               focus="password_field")

    def step_dob_open(self):
        self.dev.tap_el(self.sel, "dob_button")
        return self.dev.expect(self.sel, ["dob_picker"], 12, "dob picker")

    def _open_picker(self):
        if "dob_picker" in self.dev.state(self.sel):
            return
        self.dev.tap_el(self.sel, "dob_button")
        self.dev.expect(self.sel, ["dob_picker"], 12, "dob picker")

    def step_dob_spin(self, retries=3):
        self.log(f"   target dob = {self.dob['text']}")
        for attempt in range(retries + 1):
            try:
                self._open_picker()
                for part in ("year", "month", "day"):
                    x = DOB_WHEELS[part]
                    steps = self.dev.wheel(x, self.dob[part])
                    self.log(f"   wheel {part}: -> {self.dob[part]} OK ({steps} steps)")
                self.dev.tap_el(self.sel, "dob_confirm")
                time.sleep(0.8)
                val, _ = self.dev.resolve_name(self.sel, "dob_value")
                if val and self.dob["text"] in val.desc:
                    self.log(f"   dob set: {val.desc!r}")
                    return "register_dob"
                if val:
                    self.log(f"   !! dob shows {val.desc!r}, wanted {self.dob['text']!r}")
            except UiError as exc:
                self.log(f"   dob attempt {attempt + 1}/{retries + 1} failed: {exc}")
            if attempt < retries:
                time.sleep(0.8)
                self._open_picker()
        raise UiError("E_DOB_FAIL", f"dob never accepted (wanted {self.dob['text']})")

    def step_submit(self):
        self.dev.ensure_ready()
        node = self.dev.tap_el(self.sel, "create_account_button")[0]
        self.log(f"   tap 'Create Account' at ({node.cx}, {node.cy}) via {node.cls}")
        outcome = self._await_submit_outcome()
        self.log(f"   submit outcome: {outcome}")
        return outcome

    def step_human_check(self):
        node = self.dev.tap_el(self.sel, "human_confirm_button")[0]
        self.log(f"   tap 'Confirm' at ({node.cx}, {node.cy}) via {node.cls}")
        return self.dev.expect(self.sel, ["captcha_iframe", "captcha_wrapper"], 25,
                               "after Confirm")

    def _reach_wrapper(self):
        """Get to the native hCaptcha wrapper, dismissing the Arkose iframe."""
        st = self.dev.state(self.sel)
        if "captcha_wrapper" in st:
            return True
        if "captcha_iframe" not in st:
            self.dev.wait_state(self.sel, ["captcha_iframe", "captcha_wrapper"], 20)
            st = self.dev.state(self.sel)
        if "captcha_wrapper" in st:
            return True
        self.log(f"   opaque iframe - coord-only skip {CAPTCHA_IFRAME_SKIP}")
        self.dev.tap(*CAPTCHA_IFRAME_SKIP)
        return bool(self.dev.wait_state(self.sel, ["captcha_wrapper"], self._captcha_wait()))

    def step_captcha(self, mode="human", human_timeout=None, max_rounds=None):
        if not self._reach_wrapper():
            raise UiError("E_CAPTCHA_MISSING", "wrapper never appeared")
        self.log("   native hCaptcha wrapper up - addressable")

        if mode == "human":
            return self._wait_for_human(human_timeout)

        if mode == "skip":
            for _ in range(4):
                try:
                    self.dev.tap_el(self.sel, "captcha_skip_challenge")
                except UiError:
                    break
                time.sleep(1.5)
                for name in ("pending_verify", "logged_in"):
                    if name in self.dev.state(self.sel):
                        return name
            return "parked"

        # auto
        from core.captcha import solve as solve_captcha
        kw = {"max_rounds": max_rounds} if max_rounds else {}
        outcome = solve_captcha(self.dev, self.sel, self.log, mode="text", **kw)
        self.log(f"   captcha auto outcome: {outcome}")
        if outcome == "solved":
            return self.dev.expect(
                self.sel, ["pending_verify", "logged_in", "human_check"], 30, "post-captcha")
        return self._wait_for_human(human_timeout)

    def _wait_for_human(self, timeout=None):
        """Park and poll until the wrapper is gone. You solve it in the AVD."""
        timeout = timeout or (self.cfg.captcha.human_timeout_s if self.cfg else 600)
        if self.cfg and self.cfg.captcha.beep:
            print("\a", end="", flush=True)
        self.log(f"   >> SOLVE THE CAPTCHA on {self.dev.serial} "
                 f"(waiting up to {timeout}s)")
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.dev.state(self.sel)
            if "captcha_wrapper" not in st:
                for name in ("pending_verify", "logged_in", "human_check"):
                    if name in st:
                        self.log(f"   human solved it -> {name}")
                        return name
                self.log(f"   wrapper gone, state={st}")
                return st[0] if st else "unknown"
            if "captcha_iframe" in st and "captcha_wrapper" not in st:
                self.dev.tap(*CAPTCHA_IFRAME_SKIP)
            time.sleep(2.0)
        raise UiError("E_CAPTCHA_TIMEOUT", f"nobody solved it within {timeout}s")

    # ---- full run --------------------------------------------------------
    def run(self, captcha_mode="human", human_timeout=None, max_rounds=None):
        self.step_welcome()
        self.step_identify()
        self.step_email_next()
        self.step_name()
        self.step_form_fill()
        self.step_form_next()
        self.step_dob_open()
        self.step_dob_spin()
        outcome = self.step_submit()
        if outcome == "human_check":
            self.step_human_check()
        return self.step_captcha(mode=captcha_mode, human_timeout=human_timeout,
                                 max_rounds=max_rounds)


def build(dev, account, cfg=None, log=print):
    return Signup(dev, load_selectors(), account, log=log, cfg=cfg)