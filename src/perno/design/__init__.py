"""Tokeny designu (kolory, odstępy, typografia, ikony) wspólne dla panelu i statycznego dashboardu."""

from __future__ import annotations

import os

TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.css")


def read_tokens() -> str:
    """CSS z tokenami; panel i dashboard wstawiają go do dokumentu, więc nie trzeba osobnej trasy plików."""
    with open(TOKENS_PATH, encoding="utf-8") as f:
        return f.read()
