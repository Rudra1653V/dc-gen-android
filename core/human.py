"""Human gates. park keeps the other instance working; block serialises;
assist rescues unrecognised screens.

Console:  Enter/r resume oldest   a resume all   s scrcpy   q abort
Typed text goes to the oldest gate that asked for input.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

from core.errors import HumanAbort
from core.log import log, waiting


@dataclass
class Gate:
    kind: str
    idx: int
    message: str
    needs_input: bool = False
    timeout: float = 0
    opened: float = field(default_factory=time.time)
    payload: str | None = None
    _event: threading.Event = field(default_factory=threading.Event)

    def resume(self, payload: str | None = None) -> None:
        self.payload = payload
        self._event.set()

    def wait(self) -> str | None:
        if not self._event.wait(timeout=self.timeout or None):
            raise HumanAbort(f"gate {self.kind} timed out after {self.timeout}s")
        return self.payload


class HumanManager:
    def __init__(self, cfg, backend):
        self.cfg = cfg
        self.backend = backend
        self.parked: list[Gate] = []
        self._lock = threading.Lock()
        self._block = threading.Lock()
        self._abort = threading.Event()
        self._stop = threading.Event()

    @property
    def aborted(self) -> bool:
        return self._abort.is_set()

    def start(self) -> None:
        threading.Thread(target=self._console, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _console(self) -> None:
        print("  gates: Enter/r resume   a resume all   s scrcpy   q abort")
        while not self._stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                time.sleep(0.2)
                continue
            cmd = line.strip().lower()
            if cmd == "q":
                self._abort.set()
                for g in list(self.parked):
                    g.resume(None)
            elif cmd == "a":
                for g in list(self.parked):
                    g.resume(None)
            elif cmd == "s":
                if self.parked:
                    self.backend.window(self.parked[0].idx)
            elif cmd:
                gate = self._pop(require_input=True)
                if gate is None:
                    log("no gate is waiting for input - text dropped", "warn")
                    continue
                gate.resume(line.rstrip("\n"))
            else:
                gate = self._pop()
                if gate:
                    gate.resume(None)

    def _pop(self, require_input: bool | None = None) -> Gate | None:
        with self._lock:
            for i, g in enumerate(self.parked):
                if require_input is None or g.needs_input == require_input:
                    return self.parked.pop(i)
        return None

    def open(self, kind: str, idx: int, message: str, *,
             needs_input: bool = False, timeout: float = 0,
             mirror: bool = True) -> str | None:
        if self._abort.is_set():
            raise HumanAbort()
        gate = Gate(kind=kind, idx=idx, message=message,
                    needs_input=needs_input, timeout=timeout)
        if self.cfg.human.mode == "block":
            with self._block:
                return self._serve(gate, mirror)
        with self._lock:
            self.parked.append(gate)
        return self._serve(gate, mirror)

    def _serve(self, gate: Gate, mirror: bool) -> str | None:
        waiting(f"[inst-{gate.idx}] {gate.kind}: {gate.message}")
        self._notify(gate)
        if mirror and gate.kind in ("email_verify", "captcha", "review", "assist"):
            self.backend.window(gate.idx)
        result = gate.wait()
        with self._lock:
            if gate in self.parked:
                self.parked.remove(gate)
        if self._abort.is_set():
            raise HumanAbort()
        log(f"[inst-{gate.idx}] {gate.kind} resumed", "ok")
        return result

    def _notify(self, gate: Gate) -> None:
        url = self.cfg.human.webhook
        if not url:
            return
        threading.Thread(target=self._post, args=(url, gate), daemon=True).start()

    @staticmethod
    def _post(url: str, gate: Gate) -> None:
        try:
            import requests
            requests.post(url, timeout=10, json={
                "kind": gate.kind, "instance": gate.idx, "message": gate.message,
                "needs_input": gate.needs_input,
            })
        except Exception:
            pass