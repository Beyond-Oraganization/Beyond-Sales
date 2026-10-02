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
