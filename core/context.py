"""Shared per-run handles plus the assist helper."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.errors import DCGenError, FailureCode
from core.human import HumanManager
from core.log import log


@dataclass
class Ctx:
    cfg: object
    backend: object
    store: object
    human: HumanManager
    book: object = None
    _devices: dict = field(default_factory=dict)
    _uis: dict = field(default_factory=dict)

    def device(self, idx: int):
        from device.driver import Device
        if idx not in self._devices:
            self._devices[idx] = Device(self.backend, idx, self.cfg)
        return self._devices[idx]

    def ui(self, idx: int):
        from device.ui import Ui
        if idx not in self._uis:
            self._uis[idx] = Ui(self.device(idx), self.book)
        return self._uis[idx]

    def ensure_state(self, idx: int, wants: tuple[str, ...], message: str,
                     timeout: float = 60, assists: int = 3) -> str:
        ui = self.ui(idx)
        for attempt in range(assists + 1):
            try:
                return ui.wait_state(wants, timeout=timeout)
            except DCGenError:
                if self.cfg.human.mode != "assist" or attempt == assists:
                    raise
                ui.device.capture_failure("assist", f"wanted {wants}")
                self.human.open("assist", idx,
                                f"{message} (need one of {wants}) - press Enter",
                                timeout=self.cfg.email.verify_timeout)
                time.sleep(1)
        raise DCGenError(FailureCode.E_UI_ELEMENT_MISSING, f"never reached {wants}")