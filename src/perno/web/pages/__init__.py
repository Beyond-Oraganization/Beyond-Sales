"""Rejestr stron aplikacji.

Nowa strona: utwórz moduł `src/perno/web/pages/<nazwa>.py` z obiektem `PAGE = Page(...)`
(patrz `mailing.py`), zaimportuj go poniżej i dopisz do PAGES — pojawi się w menu.
Strona główna (`home`, trasa `/`) nie ma segmentu w menu: klika się markę w pasku. Dokumenty (`docs`) to kilka stron naraz
(`docs.PAGES`), z jedną pozycją „Pomoc” w menu.
"""

from __future__ import annotations

from perno.web.pages import automat, config, docs, export, home, leady, mailing, odpowiedzi, scraper

PAGES = [home.PAGE, mailing.PAGE, odpowiedzi.PAGE, leady.PAGE, automat.PAGE, export.PAGE, scraper.PAGE, config.PAGE, *docs.PAGES]
