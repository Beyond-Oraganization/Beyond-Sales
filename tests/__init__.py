"""Pakiet testów.

Bezpiecznik: żaden test nie może otworzyć prawdziwego połączenia SMTP (ani przez pytest, ani przez
`python -m unittest discover`). Testy wysyłki używają atrap (`tests.fixtures.FakeSmtp`) albo podmieniają
`smtplib` przez `mock.patch`, co nadpisuje poniższą blokadę tylko na czas testu.
"""

import smtplib


def _no_real_smtp(*args, **kwargs):
    raise RuntimeError("test próbował otworzyć prawdziwe połączenie SMTP — użyj atrapy (tests.fixtures.FakeSmtp)")


smtplib.SMTP = _no_real_smtp  # type: ignore[misc,assignment]
smtplib.SMTP_SSL = _no_real_smtp  # type: ignore[misc,assignment]


# Drugi bezpiecznik: żaden test nie uruchamia prawdziwego `osascript` (powiadomienie na ekranie użytkownika),
# `launchctl` (LaunchAgent), `pmset` ani `caffeinate`. Testy wstrzykują własne atrapy podprocesów; to, co zostanie
# nietknięte, kończy się błędem `OSError`, który adaptery traktują jak „polecenie niedostępne”.
from perno.notify import macos as _macos  # noqa: E402
from perno.system import launchd as _launchd  # noqa: E402
from perno.system import power as _power  # noqa: E402


def _no_real_command(*args, **kwargs):
    raise OSError("test próbował uruchomić prawdziwe polecenie systemowe — wstrzyknij atrapę (run/popen)")


_macos._system_run = _no_real_command  # type: ignore[assignment]
_launchd._system_run = _no_real_command  # type: ignore[assignment]
_power._system_run = _no_real_command  # type: ignore[assignment]
_power._system_popen = _no_real_command  # type: ignore[assignment]
