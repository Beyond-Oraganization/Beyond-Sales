"""Strona „Eksport": pobranie bazy maili jako CSV (Excel-PL) lub XLSX — całej, po filtrach albo wskazanych wierszy."""

from __future__ import annotations

from datetime import date
from typing import Optional

from perno.domain import countries
from perno.domain import statuses as st
from perno.domain.rowfilter import NO_COUNTRY, RowFilter
from perno.services import export as exporter
from perno.web import layout
from perno.web.app import HttpError, Request, Response, json_response
from perno.web.context import AppContext
from perno.web.page import Page

MAX_EXPORT_IDS = 20_000  # ≈0,5 MB JSON — mieści się w limicie żądania (2 MB)


def render(ctx: AppContext) -> str:
    return layout.render_page(
        ctx, title="Eksport", active_key="eksport",
        body=layout.read_asset("eksport.html"),
        css=layout.read_asset("eksport.css"), js=layout.read_asset("eksport.js"), kit=True,
    )


def _flag(req: Request, name: str) -> Optional[bool]:
    value = req.arg(name)
    if value == "":
        return None
    if value not in ("0", "1"):
        raise HttpError(400, f"Parametr {name} musi mieć wartość 1 albo 0.")
    return value == "1"


def _day(req: Request, name: str) -> Optional[date]:
    value = req.arg(name)
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HttpError(400, f"Parametr {name}: oczekiwano daty RRRR-MM-DD, jest {value[:20]!r}.") from None


def _filter(req: Request, ctx: AppContext) -> RowFilter:
    known_segments = set(st.SEGMENT_ORDER) | set(ctx.db.distinct_segments())
    segments = tuple(dict.fromkeys(s for s in req.args("segment") if s))
    statuses = tuple(dict.fromkeys(s for s in req.args("status") if s))
    country_codes = tuple(dict.fromkeys(c for c in req.args("country") if c))
    for segment in segments:
        if segment not in known_segments:
            raise HttpError(400, f"Nieznany segment: {segment}.")
    for status in statuses:
        if status not in st.VALID_STATUSES:
            raise HttpError(400, f"Nieznany status: {status}.")
    for code in country_codes:
        if code != NO_COUNTRY and code not in countries.COUNTRY_NAMES:
            raise HttpError(400, f"Nieznany kraj: {code[:20]}.")
    return RowFilter(
        segments=segments, statuses=statuses, countries=country_codes,
        has_email=_flag(req, "has_email"), has_body=_flag(req, "has_body"),
        sent_from=_day(req, "sent_from"), sent_to=_day(req, "sent_to"),
    )


def api_meta(req: Request, ctx: AppContext) -> Response:
    counts = ctx.db.status_counts()
    return json_response({
        "segments": ctx.db.distinct_segments(),
        "statuses": [
            {"code": code, "label": label, "color": color, "count": counts.get(code, 0)}
            for code, (label, color) in st.STATUS_META.items()
        ],
        "total": sum(counts.values()),
        "column_groups": list(exporter.COLUMN_GROUPS),
        "columns": [
            {"name": name, "label": exporter.COLUMN_INFO[name][0], "group": exporter.COLUMN_INFO[name][1]}
            for name in exporter.ALL_EXPORT_COLUMNS
        ],
        "column_presets": [
            {"key": key, "label": label, "columns": list(columns)}
            for key, label, columns in exporter.COLUMN_PRESETS
        ],
    })


def api_count(req: Request, ctx: AppContext) -> Response:
    return json_response({"count": ctx.db.count_rows(_filter(req, ctx)), "total": ctx.db.count_rows()})


def _choice(value, allowed: dict, message: str):
    if not isinstance(value, str) or value not in allowed:
        raise HttpError(400, message)
    return value


def _file_response(ctx: AppContext, spec: exporter.ExportSpec) -> Response:
    try:
        result = exporter.build_export(ctx.db, spec)
    except exporter.ExportError as exc:
        raise HttpError(400, str(exc)) from None
    return Response(
        200, content_type=result.content_type, stream=result.stream, length=result.length,
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"',
                 "X-Export-Rows": str(result.rows)},
    )


def api_export(req: Request, ctx: AppContext) -> Response:
    fmt = req.arg("format")
    if fmt not in exporter.CONTENT_TYPES:
        raise HttpError(400, "Wymagany parametr format=csv lub format=xlsx.")
    delimiter = req.arg("delimiter", "semicolon")
    if delimiter not in exporter.DELIMITERS:
        raise HttpError(400, "Parametr delimiter musi mieć wartość semicolon lub comma.")
    spec = exporter.ExportSpec(
        fmt=fmt, flt=_filter(req, ctx), bodies=req.arg("bodies") == "1",
        delimiter=exporter.DELIMITERS[delimiter],
    )
    return _file_response(ctx, spec)


def api_export_selected(req: Request, ctx: AppContext) -> Response:
    """Eksport wskazanych wierszy: `{format, delimiter, ids, columns | bodies}`. Wiersze trafiają do pliku
    w kolejności `ids` (tak, jak je widać na ekranie), kolumny — w kolejności kanonicznej."""
    body = req.json
    fmt = _choice(body.get("format"), exporter.CONTENT_TYPES, "Pole format musi mieć wartość csv albo xlsx.")
    delimiter = _choice(body.get("delimiter", "semicolon"), exporter.DELIMITERS,
                        "Pole delimiter musi mieć wartość semicolon albo comma.")
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        raise HttpError(400, "Pole ids musi być listą identyfikatorów firm.")
    if not ids:
        raise HttpError(400, "Zaznacz przynajmniej jeden wiersz do eksportu.")
    if len(ids) > MAX_EXPORT_IDS:
        raise HttpError(400, f"Za dużo wierszy naraz (maks. {MAX_EXPORT_IDS}) — zawęź filtry.")
    columns = body.get("columns")
    if columns is not None and (not isinstance(columns, list) or not all(isinstance(name, str) for name in columns)):
        raise HttpError(400, "Pole columns musi być listą nazw kolumn.")
    spec = exporter.ExportSpec(
        fmt=fmt, flt=RowFilter(ids=tuple(ids)), bodies=body.get("bodies") is True,
        delimiter=exporter.DELIMITERS[delimiter], columns=None if columns is None else tuple(columns),
    )
    return _file_response(ctx, spec)


PAGE = Page(
    key="eksport", label="Eksport", path="/eksport", render=render, order=20,
    routes={
        ("GET", "/api/export/meta"): api_meta,
        ("GET", "/api/export/count"): api_count,
        ("GET", "/api/export"): api_export,
        ("POST", "/api/export"): api_export_selected,
    },
)
