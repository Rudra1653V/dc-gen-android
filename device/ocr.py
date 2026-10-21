"""Tesseract fallback for canvas / webview regions uiautomator cannot see."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

_available: bool | None = None


def available() -> bool:
    global _available
    if _available is None:
        _available = shutil.which("tesseract") is not None
    return _available


def _prep(png: bytes, region: tuple[int, int, int, int] | None, scale: float = 2.0):
    arr = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return None
    if region:
        x, y, w, h = region
        arr = arr[max(0, y):y + h, max(0, x):x + w]
    gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh, region, scale


def find_text(png: bytes, needle: str,
              region: tuple[int, int, int, int] | None = None) -> tuple[int, int] | None:
    if not available():
        return None
    prepped = _prep(png, region)
    if prepped is None:
        return None
    img, reg, scale = prepped
    data = pytesseract.image_to_data(img, output_type=Output.DICT, lang="eng")
    target = needle.lower()
    for i, word in enumerate(data.get("text", [])):
        if target in (word or "").lower():
            x = data["left"][i] / scale + (reg[0] if reg else 0)
            y = data["top"][i] / scale + (reg[1] if reg else 0)
            w = data["width"][i] / scale
            h = data["height"][i] / scale
            return int(x + w / 2), int(y + h / 2)
    return None