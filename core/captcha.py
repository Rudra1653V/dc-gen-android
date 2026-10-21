"""hCaptcha handling for the Discord signup.

PRIMARY PATH - the accessibility text challenge, verified by hand:

    tap the three-dot (About hCaptcha & Accessibility Options)
      -> menu of 5 buttons, tap "Accessibility Challenge" (2nd)
      -> a plain, readable word appears
      -> read it (tree first, OCR second), type it, tap "Next"

R10: the word is served REPEATEDLY. hCaptcha shows one word, then another, then
another, until it decides you are human. So solve_text LOOPS - one word per
round, up to max_rounds - and only stops when the wrapper is gone.

R10b: it is paced like a person. Settle before touching anything, clear the
field, type character by character with jitter, pause before submitting, pause
between rounds. Never fire the whole thing in one burst.

R11: the answer field sits inside the challenge card, so read_challenge SKIPS the
field's own node - otherwise a stale answer gets read back as the word.

FALLBACKS
  vision - screenshot -> VLM -> JSON action list (drag / tap).
  skip   - re-roll with Skip Challenge, then park.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import time
import urllib.request
from shutil import which

from core.errors import DCGenError, FailureCode
from core.log import log
from device.driver import Device

# menu entries are ungrounded; matched by label substring, then by position
A11Y_NEEDLES = ("accessibility challenge", "accessibility text", "text challenge")

# words that appear in the wrapper chrome and are never the answer
BLOCKLIST = {
    "hcaptcha", "en", "english", "about", "skip", "next", "submit", "verify",
    "accessibility", "challenge", "drag", "the", "correct", "pipe", "to",
    "complete", "path", "options", "and", "cookie", "audio", "image", "based",
    "please", "use", "or", "if", "available", "in", "menu", "select",
    "language", "opens", "new", "window", "with", "more", "information",
    "logo", "type", "text", "you", "see", "enter", "answer", "retrieve",
    "distorted", "characters", "shown", "below", "above", "word", "code",
    "try", "again", "incorrect", "wrong", "match",
}

# the challenge card, from the grounded wrapper: prompt y=637, image y 582..1202
CHALLENGE_BOX = (200, 560, 880, 1250)

# human pacing, in seconds
HUMAN_SETTLE = (0.8, 1.6)        # look at the word before touching anything
HUMAN_PRE_SUBMIT = (0.4, 1.1)    # pause after typing, before Next
HUMAN_BETWEEN = (0.6, 1.4)       # pause between rounds
HUMAN_KEY_GAP = (0.05, 0.14)     # per-character typing gap
MAX_ROUNDS = 10

VISION_PROMPT = """You are driving an Android phone. The attached image is a
full 1080x1920 screenshot, origin top-left, showing an hCaptcha challenge.

Reply with STRICT JSON only, no prose, no code fences:
{
  "kind": "drag" | "tap" | "unknown",
  "actions": [
    {"type": "drag", "from": [x, y], "to": [x, y], "ms": 800}
    {"type": "tap",  "at": [x, y]}
  ],
  "confidence": 0.0,
  "note": "one short line"
}

Rules:
- Pixels in the screenshot's own 1080x1920 space.
- "drag the correct pipe to complete the path": exactly one drag action.
  "from" = centre of the loose pipe piece. "to" = centre of the gap.
- "click all the X": one tap action per matching tile.
- If a Verify / submit button is visible, append a tap action for it.
- If unsure, kind = "unknown" and still give your best guess.
"""


def _text(n) -> str:
    return n.get("text") or ""


def _desc(n) -> str:
    return n.get("content-desc") or ""


def _label(n) -> str:
    return _desc(n) or _text(n)


def _pause(rng, span) -> None:
    time.sleep(rng.uniform(*span))


def _in_box(n, box=CHALLENGE_BOX) -> bool:
    c = Device.centre_of(n)
    if c is None:
        return False
    return box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]


def _clean(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "")


def _same_bounds(a, b) -> bool:
    return bool(a is not None and b is not None
                and a.get("bounds") == b.get("bounds"))


def _state(ui) -> list:
    return ui.state() or []


# ---------------------------------------------------------------------------
# reading the challenge
# ---------------------------------------------------------------------------
def _ocr_tsv(png: bytes, box=CHALLENGE_BOX, psm="6"):
    """Words inside `box`, via tesseract TSV. No PIL needed."""
    if not which("tesseract"):
        return []
    t = subprocess.run(["tesseract", "-", "stdout", "--psm", psm, "tsv"],
                       input=png, capture_output=True)
    out = []
    for row in t.stdout.decode("utf-8", "replace").splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            x, y, w, h, conf = int(c[6]), int(c[7]), int(c[8]), int(c[9]), float(c[10])
        except ValueError:
            continue
        cx, cy = x + w // 2, y + h // 2
        if not (box[0] <= cx <= box[2] and box[1] <= cy <= box[3]):
            continue
        if conf < 30:
            continue
        out.append((c[11].strip(), cx, cy, conf))
    return out


def read_challenge(ui, skip=None) -> str:
    """Read the accessibility word. Tree first, OCR second.

    R11: `skip` is the answer field's own node - never read a stale answer back
    as the challenge word.
    """
    # 1. real nodes inside the card, minus the chrome and minus the field
    cands = []
    for n in Device.nodes(ui.device.dump()):
        if not _in_box(n) or not _text(n):
            continue
        if _same_bounds(n, skip):
            continue
        word = _clean(_text(n))
        if len(word) >= 3 and word.lower() not in BLOCKLIST:
            c = Device.centre_of(n)
            cands.append((_text(n).strip(), c[0], c[1]))
    if cands:
        # the word is usually the most central text node in the card
        cands.sort(key=lambda c: (abs(c[1] - 540) + abs(c[2] - 900)))
        answer = cands[0][0]
        log(f"   word from tree -> {answer!r}")
        return answer

    # 2. OCR the card, minus the chrome
    words = _ocr_tsv(ui.device.screenshot_png())
    keep = [w for w in words if w[0].lower() not in BLOCKLIST
            and len(_clean(w[0])) >= 3]
    if not keep:
        keep = words
    answer = "".join(w[0] for w in sorted(keep, key=lambda w: (w[2], w[1])))
    log(f"   word from OCR -> {answer!r} (from {[w[0] for w in keep]})")
    return answer


# ---------------------------------------------------------------------------
# the accessibility text challenge
# ---------------------------------------------------------------------------
def _three_dot(ui) -> bool:
    for name in ("captcha_menu", "captcha_about"):
        if ui.tap_if(name):
            log(f"   three-dot via {name}")
            time.sleep(1.2)
            return True
    return False


def open_accessibility(ui) -> bool:
    """Three-dot -> Accessibility Challenge. True once the word is up."""
    if not _three_dot(ui):
        log("   no three-dot on the wrapper")
        return False

    nodes = [n for n in Device.nodes(ui.device.dump())
             if n.get("clickable") == "true" and _label(n)]
    # 1. by label
    for n in nodes:
        label = _label(n).lower()
        if any(nd in label for nd in A11Y_NEEDLES):
            c = Device.centre_of(n)
            log(f"   menu item -> {_label(n)!r} at {c}")
            ui.device.tap_xy(*c)
            time.sleep(1.8)
            return True
    # 2. by position: it is the 2nd of 5
    ordered = sorted(nodes, key=lambda n: (Device.centre_of(n)[1],
                                           Device.centre_of(n)[0]))
    if len(ordered) >= 2:
        c = Device.centre_of(ordered[1])
        log(f"   menu item by position (2nd of {len(ordered)}) -> {_label(ordered[1])!r}")
        ui.device.tap_xy(*c)
        time.sleep(1.8)
        return True
    return False


def _text_challenge_up(ui) -> bool:
    return ui.exists_strict("captcha_next")


def _clear_answer(ui) -> None:
    field = ui.find("captcha_answer_field")
    if field is not None:
        ui.device.tap_xy(field.x, field.y)
        time.sleep(0.25)
    clear = ui.find("clear_button")
    if clear is not None:
        ui.device.tap_xy(clear.x, clear.y)
        time.sleep(0.2)
    else:
        for _ in range(12):
            ui.device.shell("input keyevent 67", check=False)   # DEL - never navigates


def type_like_human(dev, text, rng) -> None:
    """Character by character, with a human gap."""
    for ch in text:
        dev.type_text(ch)
        time.sleep(rng.uniform(*HUMAN_KEY_GAP))
    log(f"   typed {text!r}")


def solve_text(ui, max_rounds=MAX_ROUNDS, rng=None) -> str:
    """Loop: read one word, type it, submit, repeat until the wrapper is gone.

    hCaptcha serves the accessibility word several times in a row, so a single
    pass is not enough. Returns 'solved', 'unsolved', or 'unavailable'.
    """
    rng = rng or random
    if not open_accessibility(ui):
        return "unavailable"
    if not _text_challenge_up(ui):
        log("   no Next after opening the menu - is the text challenge up?")
        time.sleep(1.5)

    for rnd in range(1, max_rounds + 1):
        if not _text_challenge_up(ui):
            log(f"   round {rnd}: no Next - reopening the accessibility menu")
            if not open_accessibility(ui):
                return "unsolved"

        _pause(rng, HUMAN_SETTLE)          # look at the word first

        field = ui.find("captcha_answer_field")
        answer = read_challenge(ui, skip=(field.node if field else None))
        if not answer:
            log(f"   round {rnd}: nothing readable")
            _pause(rng, HUMAN_BETWEEN)
            continue

        _clear_answer(ui)
        type_like_human(ui.device, answer, rng)
        _pause(rng, HUMAN_PRE_SUBMIT)

        hit = ui.find("captcha_next")
        if hit is not None:
            log(f"   round {rnd}/{max_rounds}: {answer!r} -> Next at ({hit.x}, {hit.y})")
            ui.device.tap_xy(hit.x, hit.y)
        else:
            log(f"   round {rnd}: no Next - pressing ENTER")
            ui.device.enter()

        # wait for a pass, or for hCaptcha to hand us the next word
        deadline = time.time() + 12
        while time.time() < deadline:
            st = _state(ui)
            if "captcha_wrapper" not in st:
                for name in ("pending_verify", "logged_in", "human_check"):
                    if name in st:
                        log(f"   round {rnd}: PASSED -> {name}")
                        return "solved"
            if ui.exists_strict("captcha_error"):
                log(f"   round {rnd}: rejected, next word")
                break
            time.sleep(0.5)

        _pause(rng, HUMAN_BETWEEN)

    log(f"   gave up after {max_rounds} rounds")
    return "unsolved"


# ---------------------------------------------------------------------------
# fallback: vision
# ---------------------------------------------------------------------------
def _vision_cfg():
    return (os.environ.get("CAPTCHA_VISION_URL",
                           "https://api.openai.com/v1/chat/completions"),
            os.environ.get("CAPTCHA_VISION_KEY", ""),
            os.environ.get("CAPTCHA_VISION_MODEL", "gpt-4o-mini"))


def _extract_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None


def vision_plan(png: bytes):
    url, key, model = _vision_cfg()
    if not key:
        log("   no CAPTCHA_VISION_KEY set - skipping vision")
        return None
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ],
        }],
        "max_tokens": 500,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        log(f"   vision call failed: {exc}")
        return None
    plan = _extract_json(data["choices"][0]["message"]["content"])
    if plan:
        log(f"   plan: {plan.get('kind')} {plan.get('actions')} "
            f"(conf {plan.get('confidence')}) {plan.get('note', '')}")
    return plan


def execute_plan(dev, plan) -> bool:
    actions = plan.get("actions") or []
    for a in actions:
        if a.get("type") == "drag":
            f, t = a["from"], a["to"]
            dev.swipe(f[0], f[1], t[0], t[1], a.get("ms", 800))
            time.sleep(0.7)
        elif a.get("type") == "tap":
            dev.tap_xy(*a["at"])
            time.sleep(0.5)
    return bool(actions)


def solve_vision(ui, rounds=3) -> str:
    dev = ui.device
    for r in range(rounds):
        before = _prompt(ui)
        plan = vision_plan(dev.screenshot_png())
        if not plan or not execute_plan(dev, plan):
            return "unsolved"
        time.sleep(2.0)
        for name in ("pending_verify", "logged_in", "human_check"):
            if name in _state(ui):
                log(f"   vision solved -> {name}")
                return "solved"
        after = _prompt(ui)
        if after and after != before:
            log(f"   round {r + 1}: new challenge -> {after!r}")
            continue
    return "unsolved"


def _prompt(ui) -> str:
    hit = ui.find("captcha_prompt")
    if hit is None or hit.node is None:
        return ""
    return _label(hit.node).strip()


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------
def solve(ui, mode="auto", max_rounds=MAX_ROUNDS) -> str:
    """mode: auto | text | vision | skip | probe."""
    if mode == "skip":
        return "parked"
    if mode == "probe":
        _three_dot(ui)
        for n in [x for x in Device.nodes(ui.device.dump())
                  if x.get("clickable") == "true"]:
            log(f"      menu clickable: {_label(n)!r} at {Device.centre_of(n)}")
        return "parked"

    if mode in ("auto", "text"):
        outcome = solve_text(ui, max_rounds)
        if outcome == "solved":
            return "solved"
        if mode == "text":
            return outcome

    if mode in ("auto", "vision"):
        if solve_vision(ui) == "solved":
            return "solved"

    return "unsolved"