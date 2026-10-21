"""Randomised display names and usernames, collision-free across runs.

Word lists come from config.toml when set, otherwise from the built-ins here.
Every generated value is checked against the used-* files and appended to them
on commit, so a second run never repeats a name.
"""
from __future__ import annotations

import random
import re
from pathlib import Path

ADJECTIVES = [
    "Cosmic", "Silent", "Lunar", "Solar", "Wild", "Quiet", "Brave", "Swift",
    "Golden", "Silver", "Crimson", "Azure", "Emerald", "Frozen", "Burning",
    "Velvet", "Rustic", "Neon", "Ancient", "Modern", "Hidden", "Distant",
    "Gentle", "Fierce", "Clever", "Lucky", "Merry", "Noble", "Proud", "Witty",
    "Daring", "Calm", "Bright", "Dark", "Hollow", "Iron", "Amber", "Indigo",
    "Vivid", "Mellow", "Restless", "Curious", "Sturdy", "Brisk", "Cozy",
    "Electric", "Mystic", "Radiant", "Tiny", "Giant",
]

NOUNS = [
    "Otter", "Falcon", "Kite", "Willow", "Ember", "Comet", "Harbor", "Meadow",
    "Lantern", "Cipher", "Nomad", "Pilot", "Raven", "Sparrow", "Badger",
    "Beacon", "Bison", "Cedar", "Dune", "Echo", "Fern", "Forge", "Glacier",
    "Grove", "Heron", "Ivory", "Jasper", "Kestrel", "Lagoon", "Lynx",
    "Marble", "Nimbus", "Orchid", "Pebble", "Quartz", "Ridge", "Sable",
    "Thistle", "Tundra", "Vesper", "Walnut", "Yarrow", "Zephyr", "Anchor",
    "Bramble", "Cobalt", "Drift", "Flint", "Garnet", "Summit",
]

USERNAME_WORDS = [
    "otter", "falcon", "willow", "ember", "comet", "harbor", "meadow",
    "lantern", "cipher", "nomad", "pilot", "raven", "sparrow", "badger",
    "beacon", "cedar", "dune", "echo", "fern", "forge", "glacier", "grove",
    "heron", "jasper", "kestrel", "lagoon", "lynx", "marble", "nimbus",
    "orchid", "pebble", "quartz", "ridge", "sable", "thistle", "tundra",
    "vesper", "walnut", "zephyr", "anchor", "bramble", "cobalt", "drift",
]

USERNAME_ALLOWED = re.compile(r"[^a-z0-9._]")


def _read_set(path):
    p = Path(path)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _append(path, values):
    if not values:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for v in values:
            fh.write(v + "\n")


def _sanitize_username(s: str, max_len: int) -> str:
    s = USERNAME_ALLOWED.sub("", (s or "").lower())
    s = s.strip("._")
    return s[:max_len] or "user"


def _sanitize_display(s: str, max_len: int) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:max_len] or "User"


class NameFactory:
    def __init__(self, cfg, rng=None, log=print):
        self.cfg = cfg
        self.id = cfg.identity
        self.rng = rng or random.Random()
        self.log = log

        self.adjectives = list(self.id.name_adjectives) or ADJECTIVES
        self.nouns = list(self.id.name_nouns) or NOUNS
        self.words = list(self.id.username_words) or USERNAME_WORDS

        self.used_names = _read_set(self.id.used_names_file)
        self.used_usernames = _read_set(self.id.used_usernames_file)
        self._new_names, self._new_usernames = [], []

    # ---- display name ----------------------------------------------------
    def display_name(self, email: str = "") -> str:
        for _ in range(200):
            style = self.id.name_style
            if style == "list" and self.id.display_names:
                base = self.rng.choice(self.id.display_names)
            elif style == "email_local" and email:
                local = email.split("@")[0].split("+")[0]
                base = (re.split(r"[^A-Za-z0-9]", local)[0] or local).capitalize()
            else:
                base = f"{self.rng.choice(self.adjectives)}{self.id.name_separator}" \
                       f"{self.rng.choice(self.nouns)}"
            name = _sanitize_display(base, self.id.name_max_len)
            if name.lower() not in self.used_names:
                self.used_names.add(name.lower())
                self._new_names.append(name)
                return name
        return _sanitize_display(base, self.id.name_max_len)

    # ---- username --------------------------------------------------------
    def username(self, email: str = "") -> str:
        prefix = self.id.username_prefix
        sep = self.id.username_separator
        for _ in range(300):
            style = self.id.username_style
            if style == "email_local" and email:
                stem = _sanitize_username(email.split("@")[0].split("+")[0],
                                          self.id.username_max_len)
            elif style == "digits":
                stem = prefix or "user"
            else:
                stem = f"{prefix}{self.rng.choice(self.words)}"
            digits = "".join(self.rng.choice("0123456789")
                             for _ in range(max(0, self.id.username_digits)))
            cand = _sanitize_username(f"{stem}{sep}{digits}", self.id.username_max_len)
            if len(cand) < 3:
                cand = _sanitize_username(cand + digits, self.id.username_max_len)
            if cand not in self.used_usernames:
                self.used_usernames.add(cand)
                self._new_usernames.append(cand)
                return cand
        raise RuntimeError("could not find a free username")

    # ---- persistence -----------------------------------------------------
    def commit(self):
        _append(self.id.used_names_file, self._new_names)
        _append(self.id.used_usernames_file, self._new_usernames)
        self._new_names, self._new_usernames = [], []