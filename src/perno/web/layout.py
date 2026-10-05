"""Wspólny układ stron: <head>, górne menu zbudowane z rejestru, dane startowe dla JS."""

from __future__ import annotations

import html
import json
import os

from perno.domain import countries
from perno.domain import statuses as st

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "assets")
# Wspólny interfejs list firm (panel Wysyłka i Eksport): stan filtrów, zaznaczanie, model listy (żądania do serwera) + DOM + style.
KIT_JS = ("filters.js", "selection.js", "listmodel.js", "kit.js")
KIT_CSS = ("kit.css",)


def read_asset(name: str) -> str:
    with open(os.path.join(ASSETS_DIR, name), encoding="utf-8") as f:
        return f.read()


def _script_json(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def render_page(ctx, *, title: str, active_key: str, body: str, css: str = "", js: str = "",
                kit: bool = False) -> str:
    nav_pages = sorted((p for p in ctx.pages if p.in_nav), key=lambda p: (p.order, p.label))
    current = ' aria-current="page"'
    links = "".join(
        '<a class="topnav-link" href="{}"{}>{}</a>'.format(
            html.escape(p.path), current if p.key == active_key else "", html.escape(p.label))
        for p in nav_pages
    )
    boot = _script_json({
        "statusMeta": st.STATUS_META,
        "manualStatuses": list(st.MANUAL_STATUSES),
        "sendable": list(st.SENDABLE_STATUSES),
        "countries": [list(item) for item in countries.choices()],  # [kod, nazwa po polsku, flaga]
        "segmentOrder": list(st.SEGMENT_ORDER),
        "keepsSentAt": list(st.KEEPS_SENT_AT),
    })
    kit_css = "".join(read_asset(name) + "\n" for name in KIT_CSS) if kit else ""
    kit_js = f'<script>\n{chr(10).join(read_asset(name) for name in KIT_JS)}\n</script>\n' if kit else ""
    return (
        '<!DOCTYPE html>\n<html lang="pl">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<title>Perno — {html.escape(title)}</title>\n'
        f'<style>\n{read_asset("base.css")}\n{kit_css}{css}\n</style>\n</head>\n<body>\n'
        '<nav class="topnav">'
        '<span class="topnav-brand">Perno</span>'
        f'{links}'
        f'<span class="topnav-db" title="Baza danych">{html.escape(ctx.db.describe())}</span>'
        '</nav>\n'
        f'{body}\n'
        f'<script>\nconst APP = {boot};\n{read_asset("common.js")}\n</script>\n'
        f'{kit_js}'
        f'<script>\n{js}\n</script>\n</body>\n</html>\n'
    )
