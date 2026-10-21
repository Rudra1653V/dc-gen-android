"""Per-instance device identity, applied at boot via emulator -prop flags."""
from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass

MODELS = [
    ("Pixel 7",     "google", "Google", "panther", "13/TQ3A.230805.001/10316531"),
    ("Pixel 7 Pro", "google", "Google", "cheetah", "13/TQ3A.230805.001/10316531"),
    ("Pixel 6a",    "google", "Google", "bluejay", "13/TQ3A.230805.001/10316531"),
    ("Pixel 6",     "google", "Google", "oriole",  "13/TQ3A.230805.001/10316531"),
    ("Pixel 5",     "google", "Google", "redfin",  "11/RQ3A.211001.001/7641976"),
]

TZ_BY_REGION = {
    "us": "America/New_York", "gb": "Europe/London", "de": "Europe/Berlin",
    "fr": "Europe/Paris", "nl": "Europe/Amsterdam", "pl": "Europe/Warsaw",
    "ca": "America/Toronto", "au": "Australia/Sydney", "in": "Asia/Kolkata",
    "sg": "Asia/Singapore", "jp": "Asia/Tokyo", "br": "America/Sao_Paulo",
}


@dataclass
class Identity:
    model: str
    brand: str
    manufacturer: str
    device: str
    product: str
    fingerprint: str
    android_id: str
    imei: str

    def boot_props(self) -> dict[str, str]:
        return {
            "ro.product.model": self.model,
            "ro.product.brand": self.brand,
            "ro.product.manufacturer": self.manufacturer,
            "ro.product.device": self.device,
            "ro.product.name": self.product,
            "ro.build.fingerprint": self.fingerprint,
        }

    def short(self) -> str:
        return f"{self.brand}/{self.product}/{self.device}: {self.android_id[:8]}"


def for_instance(idx: int, seed: str = "dcgen") -> Identity:
    rng = random.Random(f"{seed}:{idx}")
    model, brand, manu, device, suffix = MODELS[idx % len(MODELS)]
    digest = hashlib.sha1(f"{seed}:{idx}:fp".encode()).hexdigest()[:16]
    return Identity(
        model=model, brand=brand, manufacturer=manu, device=device, product=device,
        fingerprint=f"{brand}/{device}/{device}:{suffix}/{digest[:8]}:user/release-keys",
        android_id=hashlib.sha1(f"{seed}:{idx}:aid".encode()).hexdigest()[:16],
        imei=f"35{rng.randint(100000, 999999)}{rng.randint(100000, 999999)}"[:15])


def timezone_for(proxy_url: str, fallback: str) -> str:
    import re
    m = re.search(r"\.([a-z]{2})[:\d]", proxy_url.lower())
    return TZ_BY_REGION.get(m.group(1), fallback) if m else fallback


def assert_distinct(identities: list[Identity]) -> None:
    seen: dict[str, int] = {}
    for i, ident in enumerate(identities):
        for key in (ident.fingerprint, ident.android_id, ident.imei):
            if key in seen and seen[key] != i:
                raise SystemExit(f"instances {seen[key]} and {i} share {key!r}")
            seen[key] = i


def as_dict(ident: Identity) -> dict:
    return asdict(ident)