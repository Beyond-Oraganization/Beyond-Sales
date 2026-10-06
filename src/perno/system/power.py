"""Zasilanie: czy Mac jest na baterii i blokada uśpienia z bezczynności (`caffeinate`) na czas okna wysyłki."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Optional

PMSET = "/usr/bin/pmset"
_system_run = subprocess.run  # podmieniane w tests/__init__.py: testy nie mogą uruchamiać prawdziwych poleceń systemu
_system_popen = subprocess.Popen
CAFFEINATE = "/usr/bin/caffeinate"
_PERCENT = re.compile(r"(\d{1,3})%")


@dataclass(frozen=True)
class PowerState:
    """Źródło zasilania: `on_battery` None = nie udało się ustalić (np. komputer stacjonarny albo brak pmset)."""
    on_battery: Optional[bool]
    percent: Optional[int] = None


def parse_battery(output: str) -> PowerState:
    """Odczytuje wynik `pmset -g batt` („Now drawing from 'Battery Power'” / „'AC Power'”)."""
    first = output.splitlines()[0] if output.strip() else ""
    on_battery = True if "Battery Power" in first else False if "AC Power" in first else None
    percent = _PERCENT.search(output)
    return PowerState(on_battery, int(percent.group(1)) if percent else None)


def read_power_state(run: Optional[Callable] = None) -> PowerState:
    """Aktualne zasilanie; błąd odczytu to „nie wiadomo”, nigdy wyjątek (to tylko ostrzeżenie dla użytkownika)."""
    try:
        done = (run or _system_run)([PMSET, "-g", "batt"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return PowerState(None)
    return parse_battery(done.stdout or "") if done.returncode == 0 else PowerState(None)


class SleepGuard:
    """Trzyma `caffeinate -i -w <pid>` (Mac nie zaśnie z bezczynności), dopóki jest potrzebny; znika razem z procesem.

    Nie powstrzyma uśpienia po zamknięciu klapy na baterii — tego macOS nie pozwala obejść bez zmiany ustawień systemu."""

    def __init__(self, popen: Optional[Callable] = None, pid: Optional[int] = None):
        self._popen = popen
        self._pid = pid or os.getpid()
        self._proc: Any = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def set_needed(self, needed: bool) -> None:
        """Włącza albo zwalnia blokadę zależnie od tego, czy trwa okno wysyłki."""
        if needed and not self.active:
            try:
                self._proc = (self._popen or _system_popen)([CAFFEINATE, "-i", "-w", str(self._pid)], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None  # brak caffeinate nie może zatrzymać wysyłki
        elif not needed and self._proc is not None:
            self.release()

    def release(self) -> None:
        """Zwalnia blokadę uśpienia."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
