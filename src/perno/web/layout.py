"""Wspólny układ stron: <head>, górne menu zbudowane z rejestru, dane startowe dla JS."""

from __future__ import annotations

import html
import json
import logging
import os

from perno.design import read_tokens
from perno.domain import countries
from perno.domain import statuses as st

logger = logging.getLogger(__name__)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "assets")
# Wspólny interfejs list firm (panel Wysyłka i Eksport): stan filtrów, zaznaczanie, model listy (żądania do serwera) + DOM + style.
KIT_JS = ("filters.js", "selection.js", "listmodel.js", "kit.js")
KIT_CSS = ("kit.css",)


def read_asset(name: str) -> str:
    with open(os.path.join(ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()


def _script_json(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _badge(page, ctx) -> int:
    """Licznik strony w menu. Błąd (np. baza chwilowo niedostępna) nie może zepsuć żadnej strony, więc kończy się zerem."""
    if page.badge is None:
        return 0
    try:
        return max(0, int(page.badge(ctx)))
    except Exception:
        logger.warning("Licznik strony %s w menu nie powiódł się", page.key, exc_info=True)
        return 0


def _nav_link(page, ctx, active_key: str) -> tuple:
    """(HTML odnośnika, licznik): z licznikiem przy nazwie, gdy jest większy od zera."""
    count = _badge(page, ctx)
    badge = f'<span class="topnav-badge" role="status" aria-label="{count} {html.escape(page.badge_label)}">{count}</span>' if count else ""
    current = ' aria-current="page"' if page.key == active_key else ""
    return f'<a class="topnav-link" href="{html.escape(page.path)}"{current}>{html.escape(page.label)}{badge}</a>', count


def render_page(ctx, *, title: str, active_key: str, body: str, css: str = "", js: str = "",
                kit: bool = False) -> str:
    nav_pages = sorted((p for p in ctx.pages if p.in_nav), key=lambda p: (p.order, p.label))
    rendered = [(_nav_link(p, ctx, active_key), p) for p in nav_pages]
    links = "".join(link for (link, _), _ in rendered)
    pending = next((count for (_, count), page in rendered if page.key == active_key), 0)
    title_text = (f"({pending}) " if pending else "") + f"Perno — {title}"
    boot = _script_json({
        "statusMeta": st.STATUS_META,
        "manualStatuses": list(st.MANUAL_STATUSES),
        "sendable": list(st.SENDABLE_STATUSES),
        "countries": [list(item) for item in countries.choices()],  # [kod, nazwa po polsku, flaga]
        "segmentOrder": list(dict.fromkeys([*st.SEGMENT_ORDER, *ctx.db.distinct_segments()])),
        "keepsSentAt": list(st.KEEPS_SENT_AT),
    })
    kit_css = "".join(read_asset(name) + "\n" for name in KIT_CSS) if kit else ""
    kit_js = f'<script>\n{chr(10).join(read_asset(name) for name in KIT_JS)}\n</script>\n' if kit else ""
    return (
        '<!DOCTYPE html>\n<html lang="pl">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<meta name="color-scheme" content="light dark">\n'
        f'<title>{html.escape(title_text)}</title>\n'
        f'<style>\n{read_tokens()}\n{read_asset("base.css")}\n{kit_css}{css}\n</style>\n</head>\n<body>\n'
        '<nav class="topnav" aria-label="Główna nawigacja">'
        '<span class="topnav-brand">Perno</span>'
        f'<div class="topnav-seg">{links}</div>'
        f'<span class="topnav-db" title="Baza danych">{html.escape(ctx.db.describe())}</span>'
        '</nav>\n'
        f'{body}\n'
        f'<script>\nconst APP = {boot};\n{read_asset("common.js")}\n</script>\n'
        f'{kit_js}'
        f'<script>\n{js}\n</script>\n</body>\n</html>\n'
    )
