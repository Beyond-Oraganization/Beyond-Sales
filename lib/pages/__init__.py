"""Rejestr stron aplikacji.

Nowa strona: utwórz moduł `lib/pages/<nazwa>.py` z obiektem `PAGE = Page(...)`
(patrz `wysylka.py`), zaimportuj go poniżej i dopisz do PAGES — pojawi się w menu.
"""

from __future__ import annotations

from lib.pages import eksport, skaper, wysylka

PAGES = [wysylka.PAGE, eksport.PAGE, skaper.PAGE]
