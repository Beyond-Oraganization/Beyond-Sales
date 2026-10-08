"""Pobieranie odpowiedzi w tle serwera panelu: jeden przebieg naraz, bez blokowania żądań HTTP.

Przebieg trwa sekundy do kilkudziesięciu sekund (jest tylko do odczytu), więc nie potrzebuje osobnego procesu jak skan stron.
Blokadę „jeden przebieg na bazę” trzyma `ReplyStore.acquire_run`, a ten uchwyt pilnuje, żeby ten sam serwer nie uruchamiał
dwóch wątków naraz.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class FetchRunner:
    """Uruchamia podane zadanie w wątku tła, o ile poprzednie się skończyło."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, job: Callable[[], object]) -> bool:
        """Startuje zadanie; False, gdy poprzednie jeszcze trwa."""
        with self._lock:
            if self.running:
                return False
            self._thread = threading.Thread(target=self._work, args=(job,), name="pobieranie-odpowiedzi", daemon=True)
            self._thread.start()
            return True

    @staticmethod
    def _work(job: Callable[[], object]) -> None:
        try:
            job()
        except Exception:
            logger.exception("Pobieranie odpowiedzi w tle nie powiodło się")

    def wait(self, timeout: float = 5.0) -> None:
        """Czeka na koniec zadania (do testów i do zamykania)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
