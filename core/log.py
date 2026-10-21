"""Colourised timestamped logging - same call contract as upstream DC-gen, plus
an optional file sink so an overnight run survives a closed terminal."""
from __future__ import annotations

import contextvars
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

_COLOUR = {"info": "\033[34m", "ok": "\033[32m", "warn": "\033[33m", "err": "\033[31m"}
_DIM, _RESET, _MAGENTA = "\033[90m", "\033[0m", "\033[35m"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_instance: contextvars.ContextVar[str] = contextvars.ContextVar("inst", default="")
_lock = threading.Lock()
_use_colour = True
_console = None
_file = None

BANNER = r"""
██████╗ ██╗██╗   ██╗███████╗██████╗
██╔══██╗██║██║   ██║██╔════╝██╔══██╗
██████╔╝██║██║   ██║█████╗  ██████╔╝
██╔══██╗██║╚██╗ ██╔╝██╔══╝  ██╔══██╗
██║  ██║██║ ╚████╔╝ ███████╗██║  ██║
╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝
"""


def set_colour(enabled: bool) -> None:
    global _use_colour
    _use_colour = enabled


def start_file(path: Path) -> Path:
    global _file
    path.parent.mkdir(parents=True, exist_ok=True)
    _file = open(path, "a", encoding="utf-8")
    log(f"logging to {path}")
    return path


def stop_file() -> None:
    global _file
    if _file:
        _file.close()
        _file = None


def bind_instance(idx: int | None):
    return _instance.set(f"[i{idx}] " if idx is not None else "")


def unbind(token) -> None:
    if token is None:                       # a discarded bind must not kill a thread
        _instance.set("")
        return
    try:
        _instance.reset(token)
    except (ValueError, TypeError):
        _instance.set("")


def _ts() -> str:
    return f"{_DIM}[{datetime.now():%H:%M:%S}]{_RESET}"


def log(msg: str, level: str = "info") -> None:
    body = f"{_instance.get()}{msg}"
    if _use_colour:
        body = f"{_COLOUR.get(level, '')}{body}{_RESET}"
    line = f"{_ts()} {body}"
    with _lock:
        if _console is not None:
            _console.print(line, highlight=False, soft_wrap=True)
        else:
            print(line, flush=True)
        if _file is not None:
            _file.write(_ANSI.sub("", line) + "\n")
            _file.flush()


def banner(subtitle: str) -> None:
    print(f"{_MAGENTA if _use_colour else ''}{BANNER}{_RESET if _use_colour else ''}")
    print(f"{_DIM if _use_colour else ''}  {subtitle}{_RESET if _use_colour else ''}\n")


def waiting(msg: str) -> None:
    log(f"WAITING_HUMAN  {msg}", "warn")


def die(msg: str, code: int = 1) -> None:
    log(msg, "err")
    raise SystemExit(code)


def enable_dashboard(console) -> None:
    global _console
    _console = console
    set_colour(console.is_terminal)