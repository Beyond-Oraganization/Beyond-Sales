"""LaunchAgent: uruchamianie demona automatu po zalogowaniu (`RunAtLoad`) i po awarii (`KeepAlive`).

Budowa pliku plist jest czystą funkcją; instalacja i zdejmowanie wołają `launchctl` przez wstrzykiwany `run`.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

LABEL = "pl.perno.automat"
LAUNCHCTL = "/bin/launchctl"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class LaunchdError(Exception):
    """`launchctl` odmówił wykonania polecenia (komunikat dla użytkownika)."""


@dataclass(frozen=True)
class LaunchdStatus:
    """Czy plik LaunchAgenta istnieje, czy jest załadowany i pod jakim pid działa."""
    installed: bool
    loaded: bool
    pid: Optional[int] = None


def plist_path(home: str) -> Path:
    """Gdzie leży plik LaunchAgenta użytkownika."""
    return Path(home) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(*, python: str, base_dir: str, log_path: str, path_env: str = DEFAULT_PATH) -> bytes:
    """Zawartość pliku plist. Demon ma wstać po zalogowaniu i po awarii, ale nie w pętli po świadomym zakończeniu
    (`KeepAlive/SuccessfulExit=false`: kod 0 = koniec, kod ≠ 0 = awaria i ponowny start po `ThrottleInterval`)."""
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "perno.cli.automat", "run"],
        "WorkingDirectory": base_dir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {"PATH": path_env, "PYTHONUNBUFFERED": "1"},
    })


def _launchctl(run: Callable, *args: str, check: bool = True):
    try:
        done = run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchdError(f"Nie udało się uruchomić launchctl: {exc}") from exc
    if check and done.returncode != 0:
        raise LaunchdError(f"launchctl {args[0]} nie powiódł się: {(done.stderr or done.stdout or '').strip()[:300]}")
    return done


def domain(uid: Optional[int] = None) -> str:
    """Domena launchd bieżącego użytkownika (sesja graficzna: tam działają powiadomienia)."""
    return f"gui/{os.getuid() if uid is None else uid}"


def status(run: Callable = subprocess.run, *, home: str, uid: Optional[int] = None) -> LaunchdStatus:
    """Stan LaunchAgenta; nie rzuca wyjątków (brak launchctl = „nie załadowany”)."""
    installed = plist_path(home).exists()
    try:
        done = _launchctl(run, "print", f"{domain(uid)}/{LABEL}", check=False)
    except LaunchdError:
        return LaunchdStatus(installed, False)
    if done.returncode != 0:
        return LaunchdStatus(installed, False)
    pid = next((int(line.split("=")[1]) for line in (done.stdout or "").splitlines()
                if line.strip().startswith("pid =") and line.split("=")[1].strip().isdigit()), None)
    return LaunchdStatus(installed, True, pid)


def install(run: Callable = subprocess.run, *, home: str, content: bytes, uid: Optional[int] = None) -> Path:
    """Zapisuje plist i ładuje go (ponowna instalacja najpierw zdejmuje poprzednią wersję)."""
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o644)
    _launchctl(run, "bootout", f"{domain(uid)}/{LABEL}", check=False)
    _launchctl(run, "bootstrap", domain(uid), str(path))
    return path


def uninstall(run: Callable = subprocess.run, *, home: str, uid: Optional[int] = None) -> bool:
    """Zdejmuje agenta i usuwa plik; zwraca, czy coś było zainstalowane."""
    path = plist_path(home)
    was = path.exists()
    _launchctl(run, "bootout", f"{domain(uid)}/{LABEL}", check=False)
    path.unlink(missing_ok=True)
    return was
