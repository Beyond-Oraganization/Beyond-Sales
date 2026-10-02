"""Generowanie treści maili z danych firm i szablonów HTML."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Sequence

from perno import settings as cfg
from perno.db import Database, EmptyDatabase
from perno.domain import forms, templating
from perno.domain import statuses as st
from perno.mailer import mime_builder


def load_seed(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def render_row(segment: str, seed: dict, template: str, sender_name: str) -> tuple:
    f = forms.conjugate(seed["mode"])
    values = {
        "obs": seed["obs"],
        "pitch": seed["pitch"],
        "channel_summary": seed["channel_summary"],
        "sender_name": sender_name or forms.SIGNATURE_TOKEN,
        "f_chcial": f["chcial"], "f_dop": f["dop"],
        "f_musi": f["musi"], "f_pyta": f["pyta"],
        "inline_image_cid": mime_builder.INLINE_IMAGE_CID,
    }
    body_html = templating.render(template, values)
    body_text = templating.html_to_text(body_html)
    cid = mime_builder.INLINE_IMAGE_CID
    inline_images = cid if f"cid:{cid}" in body_html else ""
    return seed["subject"], body_html, body_text, inline_images


def generate_missing(
    existing_index: dict, segment: str, seed_rows: list, template: str,
    sender_name: str, regenerate_pending: bool, now: datetime,
) -> tuple:
    """Zwraca (zmienione_wiersze, statystyki). Nie dotyka bazy — wiersze do
    zapisu przekazuje się do Database.upsert_generated()."""
    stamp = now.isoformat(timespec="seconds")
    stats = {"created": 0, "regenerated": 0}
    changed = []

    for seed in seed_rows:
        row_number = seed.get("row_number") or ""
        if not row_number:
            continue
        row_id = f"{segment}-{int(row_number):03d}"
        existing = existing_index.get(row_id)
        if existing and existing["status"] in st.PROTECTED_FROM_REGENERATION:
            continue
        if existing and not regenerate_pending:
            continue

        if seed.get("already_customer") == "true":
            status, subject, body_html, body_text, inline_images = (st.STATUS_SKIPPED_CUSTOMER, "", "", "", "")
        elif seed.get("needs_manual_copy") == "true":
            status, subject, body_html, body_text, inline_images = (st.STATUS_AWAITING_COPY, "", "", "", "")
        else:
            subject, body_html, body_text, inline_images = render_row(segment, seed, template, sender_name)
            status = st.STATUS_PENDING if seed.get("email") else st.STATUS_SKIPPED_NO_EMAIL

        changed.append({
            "id": row_id, "segment": segment, "row_number": row_number,
            "company": seed.get("company_display_name", ""),
            "contact_person": seed.get("contact_person_guess", ""),
            "email": seed.get("email", ""), "website": seed.get("website", ""),
            "tag": seed.get("tag", ""), "mode": seed.get("mode", ""),
            "subject": subject, "body_html": body_html, "body_text": body_text,
            "status": status, "generated_at": stamp, "sent_at": "",
            "attachments": "", "inline_images": inline_images, "error": "", "retry_count": "0",
        })
        stats["regenerated" if existing else "created"] += 1

    return changed, stats


SEGMENTS = {
    "eventy": (cfg.EVENTY_SEED, cfg.EVENTY_TEMPLATE_PATH),
    "driverzy": (cfg.DRIVERZY_SEED, cfg.DRIVERZY_TEMPLATE_PATH),
}


def refresh_segments(db: Database, segments: Sequence[str], sender_name: str, regenerate_pending: bool = False,
                     allow_empty: bool = False) -> dict:
    """Generuje brakujące wiersze dla segmentów i zapisuje je w bazie.

    Pusta baza = najpewniej niezmigrowana historia: generowanie od zera
    oznaczyłoby jako "do wysłania" firmy, które już dostały maila. Dlatego
    wymaga jawnej zgody (allow_empty)."""
    if not allow_empty and db.count_rows() == 0:
        raise EmptyDatabase(
            "Baza jest pusta. Najpierw przenieś historię: python3 migrate_csv_to_sql.py "
            "(albo, dla świeżej instalacji bez historii, użyj --allow-empty-db)."
        )
    index = db.fetch_index()
    now = db.now()
    result = {}
    for segment in segments:
        seed_path, template_path = SEGMENTS[segment]
        with open(template_path, encoding="utf-8") as f:
            template = f.read()
        changed, stats = generate_missing(
            index, segment, load_seed(seed_path), template, sender_name, regenerate_pending, now)
        result[segment] = {**stats, **db.upsert_generated(changed)}
    return result
