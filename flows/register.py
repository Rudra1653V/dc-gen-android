"""Discord signup flow, driven from a fresh Account record.

R6:  the identify screen comes up in PHONE mode - tap Email AND blur.
R7:  the create-account username is NOT reliably pre-filled; we supply our own
     randomised one and retry with a fresh candidate if it is taken.
R8:  the form's single Clear button belongs to the username - scope it by y.
R9:  the notification shade swallows every tap - ensure_ready() first.
R10: the hCaptcha accessibility text challenge is served REPEATEDLY - the solver
     loops, one word per round, at a human pace.
R11: the answer field sits inside the challenge card - read_challenge skips it.

NO keyevent 111 (ESC) via hide_keyboard is safe; ESC is not a dedicated IME key,
so it is handled as BACK when no keyboard is up. Device.hide_keyboard() only
sends it while the IME is actually shown.
"""
from __future__ import annotations

import random
import re
import time

from core import captcha as captcha_mod
from core import email as email_mod
from core import token as token_mod
from core.errors import DCGenError, FailureCode, HumanAbort
from core.log import log
from core.names import NameFactory, USERNAME_WORDS
from core.state import Account, State
from device import picker, ready
from device.driver import Device

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


def username_candidate(prefix="", digits=6, sep="", max_len=20) -> str:
    base = prefix + random.choice(USERNAME_WORDS)
    suffix = "".join(random.choice("0123456789") for _ in range(digits))
    cand = re.sub(r"[^a-z0-9._]", "", f"{base}{sep}{suffix}".lower())
    return cand[:max_len]


def make_dob(cfg) -> dict:
    d = cfg.dob
    year = d.pin_year or random.randint(d.year_min, d.year_max)
    month = d.pin_month or random.randint(1, 12)
    day = d.pin_day or random.randint(1, d.day_max)
    return {"year": year, "month": month, "day": day,
            "text": f"{month:02d}/{day:02d}/{year}"}


def build_account(cfg, store, idx: int) -> Account:
    addr = email_mod.generate(cfg, store)
    names = NameFactory(cfg)
    acct = Account(email=addr, password=cfg.account.password,
                   display_name=names.display_name(addr),
                   username=names.username(addr), instance=idx)
    names.commit()
    return acct


class Signup:
    def __init__(self, ctx, idx: int, account: Account):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.idx = idx
        self.dev: Device = ctx.device(idx)
        self.ui = ctx.ui(idx)
        self.acct = account
        self.dob = make_dob(self.cfg)
        self.username = account.username

    # ---- convenience -----------------------------------------------------
    @property
    def email(self):
        return self.acct.email

    @property
    def password(self):
        return self.acct.password

    @property
    def name(self):
        return self.acct.display_name

    # ---- helpers ---------------------------------------------------------
    def _key(self, code: int) -> None:
        self.dev.shell(f"input keyevent {code}", check=False)
        self.dev.invalidate()

    def _ready(self) -> None:
        if not ready.ensure_ready(self.dev):
            log(f"   !! overlay stuck: {self.dev.foreground()!r}", "warn")

    def hide_keyboard(self) -> None:
        out = self.dev.shell("dumpsys input_method | grep -m1 mInputShown", check=False)
        if "mInputShown=true" in out:
            self._key(4)                      # BACK, only while the IME is up
            time.sleep(0.5)

    def _blur(self) -> None:
        self.dev.tap_xy(*BLUR_POINT)
        time.sleep(0.5)

    def _state(self) -> list:
        return self.ui.state() or []

    def _field_text(self, name: str) -> str | None:
        hit = self.ui.find(name, strict=True)
        if hit is None or hit.node is None:
            return None
        return hit.node.get("text") or ""

    def _clear_field(self, hit, value: str) -> None:
        clear = self.ui.find("clear_button")
        if clear is not None and abs(clear.y - hit.y) <= CLEAR_TOLERANCE:
            self.dev.tap_xy(clear.x, clear.y)
            time.sleep(0.3)
            return
        for _ in range(len(value) + 4):
            self._key(67)                     # DEL - never navigates

    def _fill(self, name, value, verify=True, masked=False, soft=False, tries=2):
        want = FIELD_STATE.get(name)
        for attempt in range(1, tries + 1):
            if want:
                self.ui.wait_state([want], 15)
            hit = self.ui.find(name, strict=True)
            if hit is None:
                raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                                 f"{name} not on screen (state={self._state()})")
            self.dev.tap_xy(hit.x, hit.y)
            time.sleep(0.5)
            self._clear_field(hit, value)
            self.dev.type_text(value)
            time.sleep(0.5)
            if not verify:
                return
            got = self._field_text(name)
            if got is None:
                raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                                 f"{name} vanished after typing")
            if masked:
                bad = len(got) != len(value)
                detail = f"holds {len(got)} chars, wanted {len(value)}"
            else:
                bad = got != value
                detail = f"reads {got!r}, wanted {value!r}"
            if not bad:
                log(f"   filled {name} = {got!r}")
                return
            if attempt < tries:
                log(f"   !! {name} {detail} - retrying")
                time.sleep(0.5)
                continue
            if soft:
                log(f"   !! {name} {detail}")
            else:
                raise DCGenError(FailureCode.E_UI_FIELD, f"{name} {detail}")

    def _phone_mode(self) -> bool:
        return self.ui.exists_strict("country_code")

    def _use_email_tab(self, timeout=6):
        if not self._phone_mode():
            return True
        for attempt in (1, 2):
            self.ui.tap_if("email_tab")
            if self.ui.wait_gone_ok("country_code", timeout):
                log("   email tab engaged")
                return True
            log(f"   email tab tap {attempt} did not take")
        log("   !! still phone mode - relying on blur to force the switch")
        return False

    def _await_email_mode(self, timeout=8) -> bool:
        if self.ui.wait_gone_ok("country_code", timeout):
            return True
        log("   !! field never left phone mode")
        return False

    def _recover_to(self, want) -> bool:
        if want in self._state():
            return True
        st = self._state()
        log(f"   !! on {st}, want {want}")
        if "welcome" in st:
            self.step_welcome()
            return want in self._state()
        return False

    def _log_clickables(self) -> None:
        for n in [x for x in Device.nodes(self.dev.dump())
                  if x.get("clickable") == "true"][:10]:
            log(f"      clickable: {Device.label(n)!r} at {Device.centre_of(n)}")

    def _tap_until(self, element, expect_state, retries=None, timeout=12, focus=None):
        retries = retries or self.cfg.flow.retries + 1
        for attempt in range(retries):
            self._ready()
            self.hide_keyboard()
            if attempt == 2:
                if focus:
                    hit = self.ui.find(focus)
                    if hit is not None:
                        log(f"   re-focusing {focus}")
                        self.dev.tap_xy(hit.x, hit.y)
                        time.sleep(0.3)
                log("   trying ENTER")
                self.dev.enter()

            hit = self.ui.find(element, strict=True)
            if hit is None:
                log(f"   no {element} on screen")
            elif attempt == 1:
                log(f"   long-press {element} at ({hit.x}, {hit.y})")
                self.dev.swipe(hit.x, hit.y, hit.x, hit.y, 150)
            else:
                log(f"   tap {element} at ({hit.x}, {hit.y}) via {hit.by}")
                self.dev.tap_xy(hit.x, hit.y)

            got = self.ui.wait_state([expect_state, "welcome", "error_modal"], timeout)
            if got == expect_state:
                time.sleep(0.4)
                return got
            if got == "error_modal":
                return got
            log(f"   {element} attempt {attempt + 1}/{retries} did not advance "
                f"(state={self._state()})")
            if attempt == 0 and focus:
                n = self.ui.find(focus)
                if n is not None:
                    log(f"   re-focusing {focus}")
                    self.dev.tap_xy(n.x, n.y)
            if attempt == retries - 1:
                self._log_clickables()
            time.sleep(0.8)
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                         f"{expect_state} never reached; state={self._state()}")

    def _await_submit_outcome(self) -> str:
        window = self.cfg.flow.submit_timeout_s
        for r in range(3):
            deadline = time.time() + window
            while time.time() < deadline:
                st = self._state()
                for name in ("human_check", "captcha_wrapper", "captcha_iframe",
                             "pending_verify", "logged_in", "dob_error_modal"):
                    if name in st:
                        return name
                time.sleep(0.8)
            log(f"   submit still pending, round {r + 1}/3")
        raise DCGenError(FailureCode.E_SUBMIT_TIMEOUT
                         if hasattr(FailureCode, "E_SUBMIT_TIMEOUT")
                         else FailureCode.E_UI_ELEMENT_MISSING,
                         f"no post-submit screen after 3 x {window}s")

    # ---- steps -----------------------------------------------------------
    def step_welcome(self):
        return self._tap_until("register_button", "identify", 2, 20)

    def step_identify(self, tries=3):
        last = None
        for attempt in range(1, tries + 1):
            st = self._state()
            if "welcome" in st or "identify" not in st:
                log(f"   identify attempt {attempt}: state={st}")
                if "welcome" in st:
                    self.step_welcome()
                else:
                    self._recover_to("identify")
            self._use_email_tab()
            try:
                self._fill("email_field", self.email, tries=1)
            except DCGenError as exc:
                last = exc
                log(f"   identify attempt {attempt} failed: {exc}")
                time.sleep(0.6)
                continue
            self._blur()
            self._await_email_mode()
            if "identify" in self._state():
                return "identify"
            last = DCGenError(FailureCode.E_UI_FIELD,
                              f"identify lost, state={self._state()}")
            time.sleep(0.6)
        raise last or DCGenError(FailureCode.E_UI_FIELD, "identify never stuck")

    def step_email_next(self, retries=2):
        for attempt in range(retries + 1):
            self._ready()
            if not self._recover_to("identify"):
                raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                                 f"not on identify, state={self._state()}")
            hit = self.ui.tap("email_next_button")
            log(f"   tap 'Next' at ({hit.x}, {hit.y}) via {hit.by}")
            got = self.ui.wait_state(["register_name", "phone_error"], 12)
            if got == "register_name":
                time.sleep(0.4)
                return "register_name"
            if got == "phone_error":
                log(f"   !! validated in phone mode (attempt {attempt + 1}) - retrying")
                self._use_email_tab()
                self._fill("email_field", self.email)
                self._blur()
                self._await_email_mode()
                continue
            raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING,
                             f"Next did nothing, state={self._state()}")
        raise DCGenError(FailureCode.E_EMAIL_INVALID, "Next never advanced past identify")

    def step_name(self):
        self._fill("display_name_field", self.name, soft=True)
        self._blur()
        return self._tap_until("name_next_button", "register_form", 3, 12,
                               focus="display_name_field")

    def _ensure_username(self, tries=4):
        """Use the account's randomised username; retry with a fresh candidate
        only if Discord rejects it as taken."""
        hit = self.ui.find("username_field", strict=True)
        if (hit is not None and (hit.node.get("text") or "").strip()
                and not self.ui.exists_strict("username_hint")):
            self.username = (hit.node.get("text") or "").strip()
            log(f"   username pre-filled = {self.username!r}")
            return self.username

        for attempt in range(1, tries + 1):
            candidate = self.username if attempt == 1 else username_candidate()
            log(f"   typing username {candidate!r}")
            self._fill("username_field", candidate, soft=True)
            self._blur()
            if self.ui.exists_strict("username_ok"):
                self.username = self._field_text("username_field") or candidate
                log(f"   username accepted = {self.username!r}")
                self.acct.username = self.username
                return self.username
            if self.ui.exists_strict("username_bad"):
                log(f"   {candidate!r} rejected - retrying")
                continue
            log(f"   no verdict on {candidate!r} (attempt {attempt}/{tries})")
        self.username = self._field_text("username_field") or self.username
        self.acct.username = self.username
        return self.username

    def step_form_fill(self):
        self.ui.wait_state(["register_form"], 12)
        self._ensure_username()
        self._fill("password_field", self.password, masked=True)
        self.hide_keyboard()
        time.sleep(0.5)
        got = self._field_text("password_field")
        if got is None or len(got) != len(self.password):
            log(f"   !! password reads {got!r} after blur - refilling")
            self._fill("password_field", self.password, masked=True)
            self.hide_keyboard()
            time.sleep(0.5)
        return "register_form"

    def step_form_next(self):
        return self._tap_until("form_next_button", "register_dob", 3, 15,
                               focus="password_field")

    def step_dob_open(self):
        self.ui.tap("dob_button")
        return self.ui.wait_state(["dob_picker"], 12)

    def _open_picker(self):
        if "dob_picker" in self._state():
            return
        self.ui.tap("dob_button")
        self.ui.wait_state(["dob_picker"], 12)

    def step_dob_spin(self, retries=3):
        log(f"   target dob = {self.dob['text']}")
        for attempt in range(retries + 1):
            try:
                self._open_picker()
                for part in ("year", "month", "day"):
                    steps = picker.spin(self.dev, DOB_WHEELS[part], self.dob[part])
                    log(f"   wheel {part}: -> {self.dob[part]} OK ({steps} steps)")
                self.ui.tap("dob_confirm")
                time.sleep(0.8)
                val = self.ui.find("dob_value")
                desc = (val.node.get("content-desc") or "") if val and val.node else ""
                if self.dob["text"] in desc:
                    log(f"   dob set: {desc!r}")
                    return "register_dob"
                if desc:
                    log(f"   !! dob shows {desc!r}, wanted {self.dob['text']!r}")
            except DCGenError as exc:
                log(f"   dob attempt {attempt + 1}/{retries + 1} failed: {exc}")
            if attempt < retries:
                time.sleep(0.8)
                self._open_picker()
        raise DCGenError(FailureCode.E_DOB_FAIL,
                         f"dob never accepted (wanted {self.dob['text']})")

    def step_submit(self):
        self._ready()
        hit = self.ui.tap("create_account_button")
        log(f"   tap 'Create Account' at ({hit.x}, {hit.y}) via {hit.by}")
        outcome = self._await_submit_outcome()
        log(f"   submit outcome: {outcome}")
        return outcome

    def step_human_check(self):
        hit = self.ui.tap("human_confirm_button")
        log(f"   tap 'Confirm' at ({hit.x}, {hit.y}) via {hit.by}")
        return self.ui.wait_state(["captcha_iframe", "captcha_wrapper"], 25)

    def _reach_wrapper(self) -> bool:
        """Get to the native hCaptcha wrapper, dismissing the Arkose iframe."""
        st = self._state()
        if "captcha_wrapper" in st:
            return True
        if "captcha_iframe" not in st:
            self.ui.wait_state(["captcha_iframe", "captcha_wrapper"], 20)
            st = self._state()
        if "captcha_wrapper" in st:
            return True
        log(f"   opaque iframe - coord-only skip {CAPTCHA_IFRAME_SKIP}")
        self.dev.tap_xy(*CAPTCHA_IFRAME_SKIP)
        try:
            self.ui.wait_state(["captcha_wrapper"], self.cfg.flow.captcha_wait_s)
            return True
        except DCGenError:
            return False

    def step_captcha(self, mode="human", human_timeout=None, max_rounds=None):
        if not self._reach_wrapper():
            raise DCGenError(FailureCode.E_CAPTCHA_UNSOLVED, "wrapper never appeared")
        log("   native hCaptcha wrapper up - addressable")

        if mode == "human":
            return self._wait_for_human(human_timeout)

        if mode == "skip":
            for _ in range(4):
                if not self.ui.tap_if("captcha_skip_challenge"):
                    break
                time.sleep(1.5)
                for name in ("pending_verify", "logged_in"):
                    if name in self._state():
                        return name
            return "parked"

        outcome = captcha_mod.solve(self.ui, mode="text",
                                    max_rounds=max_rounds or self.cfg.captcha.auto_rounds)
        log(f"   captcha auto outcome: {outcome}")
        if outcome == "solved":
            return self.ui.wait_state(["pending_verify", "logged_in", "human_check"], 30)
        return self._wait_for_human(human_timeout)

    def _wait_for_human(self, timeout=None):
        """Park and poll until the wrapper is gone. You solve it in the AVD."""
        timeout = timeout or self.cfg.captcha.human_timeout_s
        if self.cfg.captcha.beep:
            print("\a", end="", flush=True)
        log(f"   >> SOLVE THE CAPTCHA on {self.dev.serial} "
            f"(waiting up to {timeout}s)", "warn")
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._state()
            if "captcha_wrapper" not in st:
                for name in ("pending_verify", "logged_in", "human_check"):
                    if name in st:
                        log(f"   human solved it -> {name}", "ok")
                        return name
                log(f"   wrapper gone, state={st}")
                return st[0] if st else "unknown"
            if "captcha_iframe" in st and "captcha_wrapper" not in st:
                self.dev.tap_xy(*CAPTCHA_IFRAME_SKIP)
            time.sleep(2.0)
        raise DCGenError(FailureCode.E_CAPTCHA_UNSOLVED,
                         f"nobody solved it within {timeout}s")

    # ---- full run --------------------------------------------------------
    def run(self):
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
        return self.step_captcha(mode=self.cfg.captcha.mode)


def run(ctx, idx: int) -> Account:
    """Drive one account to EMAIL_VERIFY_PENDING and park it."""
    cfg = ctx.cfg
    dev = ctx.device(idx)
    acct = build_account(cfg, ctx.store, idx)
    log(f"=== {acct.email}  name={acct.display_name!r} user={acct.username!r}")

    try:
        if cfg.flow.fresh_per_account:
            dev.clear_app()
        ready.ensure_ready(dev)
        dev.start_app()
        outcome = Signup(ctx, idx, acct).run()
        log(f"   signup outcome: {outcome}")

        acct.mark(State.SUBMITTED)
        if outcome in ("pending_verify", "logged_in", "parked", "unknown"):
            acct.mark(State.TOKEN_CAPTURED)
            token, me = token_mod.capture(cfg, dev, acct.email, acct.password)
            acct.token = token
            acct.user_id = str(me.get("id", ""))
            ctx.store.save_token(acct)

        acct.mark(State.EMAIL_VERIFY_PENDING)
        ctx.store.park(acct, "pending")
        log(f"parked for verification: {acct.email}", "ok")
        return acct
    except HumanAbort:
        raise
    except DCGenError as exc:
        acct.fail(exc.code, str(exc))
        ctx.store.save_record(acct)
        raise
    except Exception as exc:
        acct.fail(FailureCode.E_UNKNOWN, f"{type(exc).__name__}: {exc}")
        ctx.store.save_record(acct)
        raise