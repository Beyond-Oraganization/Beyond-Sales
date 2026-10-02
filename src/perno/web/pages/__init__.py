"""Rejestr stron aplikacji.

Nowa strona: utwórz moduł `src/perno/web/pages/<nazwa>.py` z obiektem `PAGE = Page(...)`
(patrz `mailing.py`), zaimportuj go poniżej i dopisz do PAGES — pojawi się w menu.
"""

from __future__ import annotations

from perno.web.pages import export, mailing, scraper

PAGES = [mailing.PAGE, export.PAGE, scraper.PAGE]
