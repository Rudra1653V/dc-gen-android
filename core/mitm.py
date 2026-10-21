"""mitmproxy per instance, chained through our own forwarder so rotation works."""
from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

from core.log import log
from core.proxy import Forwarders

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,7}\.[A-Za-z0-9_-]{25,}")

_procs: dict[int, subprocess.Popen] = {}
_ports: dict[int, int] = {}
_workdir: Path | None = None
_lock = threading.Lock()


def _tokens_file() -> Path:
    assert _workdir is not None
    return _workdir / "mitm-tokens.json"


def last_token(idx: int) -> str | None:
    if _workdir is None:
        return None
    path = _tokens_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    with _lock:
        return data.get(str(_ports.get(idx, "")))


_ADDON = """
from mitmproxy import http
import json, pathlib, re

TOK = re.compile(r'{pattern}')
OUT = pathlib.Path({out!r})
KEY = {key!r}


def response(flow: http.HTTPFlow) -> None:
    if 'discord.com/api' not in flow.request.pretty_url:
        return
    blob = flow.response.get_text(strict=False) or ''
    hit = TOK.search(blob)
    if not hit:
        return
    data = {{}}
    if OUT.exists():
        try:
            data = json.loads(OUT.read_text())
        except Exception:
            data = {{}}
    data[KEY] = hit.group(0)
    OUT.write_text(json.dumps(data))
"""


def start(cfg, idx: int, port: int, fwd: Forwarders, workdir: Path) -> None:
    global _workdir
    _workdir = workdir
    workdir.mkdir(parents=True, exist_ok=True)
    upstream_port = port + 1000
    fwd.ensure(upstream_port)

    script = workdir / f"mitm-{idx}.py"
    script.write_text(_ADDON.format(pattern=TOKEN_RE.pattern,
                                    out=str(_tokens_file()), key=str(port)))
    cmd = ["mitmdump", "-q", "--listen-port", str(port),
           "--mode", f"upstream:http://127.0.0.1:{upstream_port}", "-s", str(script)]
    logfile = open(cfg.paths.logs / f"mitm-{idx}.log", "ab")
    _procs[idx] = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    _ports[idx] = port
    log(f"mitmproxy on 127.0.0.1:{port} -> forwarder 127.0.0.1:{upstream_port}")


def stop_all() -> None:
    for proc in _procs.values():
        try:
            proc.terminate()
        except Exception:
            pass
    _procs.clear()