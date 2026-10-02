#!/usr/bin/env python3
"""Uzupełnia tabelę `maile` (e-mail, temat, treść) firmami z zakończonym skanem stron.

  python3 wypelnij.py lista                       # które firmy się kwalifikują (skan zakończony, brak maila w tabeli)
  python3 wypelnij.py kontekst --out DIR          # po jednym pliku .md na firmę: opis, kontakty, treść stron
  python3 wypelnij.py zapisz tresci.json --dry-run   # walidacja i podgląd, nic nie zapisuje
  python3 wypelnij.py zapisz tresci.json
  python3 wypelnij.py zapisz tresci.json --takze-przerwane   # także skan przerwany przez serwer (≥10 stron)
  python3 wypelnij.py zapisz tresci.json --ponownie   # przelicz też wiersze „Do wysłania”, których jeszcze nie wysłano

`tresci.json` to lista obiektów: {id, email, subject, mode, tag, obs, pitch, channel_summary}
(+ opcjonalnie contact_person, city, address, description). Pola obs/pitch/channel_summary/subject muszą być
w języku kraju siedziby firmy (PL→polski, GB/IE→angielski, DE/AT→niemiecki, ES→hiszpański); szablon wybiera się
z kraju zapisanego w tabeli. channel_summary to czynność w bezokoliczniku z dopełniaczem (po „Nie musi już Pan…”: „potwierdzać rezerwacji”,
nie „rezerwacje”) i BEZ płatności (szablon sam dopisuje „i osobno rozliczać płatności”), a pitch kończy się bezokolicznikiem („…wprowadzić płatność online”). Adres e-mail musi pochodzić z kontaktów znalezionych przez skraper (nie wymyślamy adresów).
Wiersze wysłane / odpowiedziane / „Nie kontaktować” / już klient nie są ruszane. Przed zapisem powstaje kopia w backup/.
Firma po uzupełnieniu dostaje status „Do wysłania”; wysyłka nadal wymaga Twojej decyzji (panel albo send_emails.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import sqlalchemy as sa

from perno import settings as cfg
from perno.db import Database, DbError, maile
from perno.domain import localize, templating
from perno.domain import statuses as st
from perno.scraper import statuses as sst
from perno.scraper.schema import firmy, firmy_maile, kontakty, strony, strony_tresc
from perno.services import firms_import as firms
from perno.services.generation import render_row
from perno.settings import load_config

FILLABLE = (st.STATUS_SKIPPED_NO_EMAIL, st.STATUS_AWAITING_COPY)
REQUIRED = ("id", "email", "subject", "obs", "pitch", "channel_summary")
MIN_PARTIAL_PAGES = 10  # skan przerwany przez serwer (429/503) wystarcza, gdy zdążył pobrać tyle stron
KEY_PAGE_HINTS = ("zapis", "rezerw", "cennik", "kontakt", "oferta", "termin", "kurs", "wyjazd", "wyprawy", "o-nas", "onas",
                  "booking", "contact", "price", "preise", "buchen", "reserva", "precio", "about")


def open_db(args) -> Database:
    return Database.from_config(load_config(args.config))


def candidates(db: Database, include_pending: bool = False, include_partial: bool = False) -> list:
    statuses = FILLABLE + ((st.STATUS_PENDING,) if include_pending else ())
    scans = (sst.FIRMA_ZAKONCZONA, sst.FIRMA_LIMIT) + ((sst.FIRMA_ZABLOKOWANA,) if include_partial else ())
    with db.engine.connect() as conn:
        rows = conn.execute(
            sa.select(maile.c.id, maile.c.segment, maile.c.company, maile.c.website, maile.c.country, maile.c.city,
                      maile.c.address, maile.c.description, maile.c.email, maile.c.status,
                      firmy.c.id.label("firma_id"), firmy.c.skan_status, firmy.c.stron_pobranych)
            .select_from(maile.join(firmy_maile, firmy_maile.c.maile_id == maile.c.id)
                         .join(firmy, firmy.c.id == firmy_maile.c.firma_id))
            .where(firmy.c.skan_status.in_(scans), maile.c.status.in_(statuses),
                   firmy.c.stron_pobranych >= (MIN_PARTIAL_PAGES if include_partial else 0))
            .order_by(maile.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]


def scraped_emails(db: Database, firma_id: int) -> list:
    with db.engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            sa.select(kontakty.c.wartosc, kontakty.c.w_domenie, kontakty.c.rola, kontakty.c.zrodlo_url)
            .where(kontakty.c.firma_id == firma_id, kontakty.c.typ == "email")
            .order_by(kontakty.c.w_domenie.desc(), kontakty.c.liczba_stron.desc())
        ).mappings()]


def cmd_lista(args) -> int:
    db = open_db(args)
    rows = candidates(db)
    print(f"Kwalifikuje się {len(rows)} firm (skan zakończony, w tabeli maile bez treści):")
    for r in rows:
        lang = localize.language_for(r["country"])
        emails = len(scraped_emails(db, r["firma_id"]))
        print(f"  {r['id']:<14} {r['company'][:34]:<34} kraj={r['country'] or '—':<3} język={lang or 'BRAK'}  e-maile ze skanu={emails}  stron={r['stron_pobranych']}")
    return 0


def cmd_kontekst(args) -> int:
    db = open_db(args)
    os.makedirs(args.out, exist_ok=True)
    rows = candidates(db)
    if args.id:
        rows = [r for r in rows if r["id"] in args.id]
    for r in rows:
        lines = [f"# {r['id']} — {r['company']}", "",
                 f"strona: {r['website']}", f"kraj: {r['country']}  (język maila: {localize.language_for(r['country'])})",
                 f"miasto: {r['city']}  adres: {r['address']}", f"opis w bazie: {r['description']}", "", "## E-maile ze skanu"]
        for e in scraped_emails(db, r["firma_id"]):
            lines.append(f"- {e['wartosc']}  (w domenie: {bool(e['w_domenie'])}, rola: {e['rola']}, źródło: {e['zrodlo_url']})")
        with db.engine.connect() as conn:
            for typ in ("telefon", "formularz"):
                vals = [v[0] for v in conn.execute(sa.select(kontakty.c.wartosc).where(
                    kontakty.c.firma_id == r["firma_id"], kontakty.c.typ == typ).limit(8))]
                lines += ["", f"## {typ.capitalize()}"] + [f"- {v}" for v in vals]
            lines += ["", "## Strony"]
            pages = conn.execute(
                sa.select(strony.c.url, strony.c.tytul, strony.c.opis, strony_tresc.c.tekst)
                .select_from(strony.outerjoin(strony_tresc, strony_tresc.c.strona_id == strony.c.id))
                .where(strony.c.firma_id == r["firma_id"], strony.c.status == "pobrano")
                .order_by(strony.c.glebokosc, strony.c.id)
            ).all()
        for url, title, desc, text in pages:
            lines.append(f"- {url} | {title} | {(desc or '')[:160]}")
        budget = args.chars
        for url, title, desc, text in pages:
            if budget <= 0:
                break
            if not text or not (url.rstrip("/").count("/") <= 2 or any(h in url.lower() for h in KEY_PAGE_HINTS)):
                continue
            chunk = text[:min(2500, budget)]
            budget -= len(chunk)
            lines += ["", f"### {url}", chunk]
        with open(os.path.join(args.out, f"{r['id']}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"Zapisano {len(rows)} plików w {args.out}")
    return 0


def build_updates(db: Database, entries: list, sender_name: str, include_pending: bool = False,
                  include_partial: bool = False) -> tuple:
    """(plan, błędy): plan = [(wpis, wartości do zapisu)]; nic nie dotyka bazy poza odczytem."""
    by_id = {r["id"]: r for r in candidates(db, include_pending, include_partial)}
    plan, errors, seen = [], [], set()
    for entry in entries:
        rid = entry.get("id", "?")
        def fail(msg):
            errors.append(f"{rid}: {msg}")  # noqa: B023 — wywoływane w tej samej iteracji, rid nie zdąży się zmienić
        missing = [k for k in REQUIRED if not str(entry.get(k) or "").strip()]
        if missing:
            fail("brak pól: " + ", ".join(missing)); continue
        if rid in seen:
            fail("powtórzony id w pliku"); continue
        seen.add(rid)
        row = by_id.get(rid)
        if row is None:
            fail("nie kwalifikuje się (brak wiersza, skan niezakończony albo status inny niż „Brak e-maila”/„Czeka na treść”)"); continue
        lang = localize.language_for(row["country"])
        if lang is None:
            fail(f"kraj „{row['country']}” bez szablonu językowego — ustaw kraj albo dodaj język w src/perno/domain/localize.py"); continue
        email = entry["email"].strip()
        if email.lower() not in {e["wartosc"].lower() for e in scraped_emails(db, row["firma_id"])}:
            fail(f"adres {email} nie występuje wśród kontaktów ze skanu"); continue
        texts = [entry["subject"], entry["obs"], entry["pitch"], entry["channel_summary"]]
        if lang != "pl" and localize.looks_polish(*texts):
            fail(f"treść ma polskie litery, a mail ma być po {localize.LANGUAGE_NAMES[lang]}"); continue
        with open(localize.template_path(row["segment"], lang), encoding="utf-8") as fh:
            template = fh.read()
        seed = {"mode": entry.get("mode") or "Panstwo", "obs": entry["obs"].strip(), "pitch": entry["pitch"].strip(),
                "channel_summary": entry["channel_summary"].strip(), "subject": entry["subject"].strip()}
        try:
            subject, body_html, body_text, inline_images = render_row(row["segment"], seed, template, sender_name)
        except templating.MissingTokenError as exc:
            fail(str(exc)); continue
        values = {"email": email, "subject": subject, "body_html": body_html, "body_text": body_text,
                  "inline_images": inline_images, "mode": seed["mode"], "tag": (entry.get("tag") or "").strip(),
                  "contact_person": (entry.get("contact_person") or "").strip()}
        for key in ("city", "address", "description"):
            if entry.get(key) and not (row[key] or "").strip():
                values[key] = entry[key].strip()
        plan.append((entry, {**values, "_lang": lang, "_company": row["company"]}))
    return plan, errors


def cmd_zapisz(args) -> int:
    with open(args.json, encoding="utf-8") as fh:
        entries = json.load(fh)
    db = open_db(args)
    config = load_config(args.config)
    plan, errors = build_updates(db, entries, config.get("SENDER_NAME", ""), args.ponownie, args.takze_przerwane)
    for entry, v in plan:
        print(f"  OK  {entry['id']:<14} {v['_company'][:30]:<30} [{v['_lang']}] {v['email']:<34} „{v['subject']}”")
    for e in errors:
        print(f"  BŁĄD {e}", file=sys.stderr)
    if errors:
        print(f"Przerwano: {len(errors)} błędów, nic nie zapisano (popraw plik i uruchom ponownie).", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"--dry-run: {len(plan)} wierszy gotowych do zapisu, nic nie zapisano.")
        return 0
    path = firms.backup_table(db, cfg.BACKUP_DIR, "uzupelnieniem-ze-skanu")
    print(f"Kopia zapasowa bazy: {path}")
    allowed = FILLABLE + ((st.STATUS_PENDING,) if args.ponownie else ())
    now, written = db.now(), 0
    with db.engine.begin() as conn:
        for entry, v in plan:
            values = {k: val for k, val in v.items() if not k.startswith("_")}
            values.update(status=st.STATUS_PENDING, generated_at=now, updated_at=now, error=None, sent_at=None)
            result = conn.execute(sa.update(maile).where(maile.c.id == entry["id"], maile.c.status.in_(allowed)).values(**values))
            written += result.rowcount
    print(f"Zapisano {written} z {len(plan)} wierszy (status „Do wysłania”).")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=cfg.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lista")
    ctx = sub.add_parser("kontekst")
    ctx.add_argument("--out", required=True)
    ctx.add_argument("--id", nargs="*")
    ctx.add_argument("--chars", type=int, default=9000, help="Limit treści stron na firmę (znaki).")
    save = sub.add_parser("zapisz")
    save.add_argument("json")
    save.add_argument("--dry-run", action="store_true")
    save.add_argument("--takze-przerwane", action="store_true",
                      help="Uwzględnij firmy, których skan przerwał serwer (429/503), jeśli pobrano ≥10 stron. Serwerów, które odmówiły dostępu, nie ponawiamy.")
    save.add_argument("--ponownie", action="store_true", help="Przelicz też wiersze „Do wysłania” (nigdy niewysłane).")
    args = parser.parse_args(argv)
    try:
        return {"lista": cmd_lista, "kontekst": cmd_kontekst, "zapisz": cmd_zapisz}[args.command](args)
    except DbError as exc:
        print(f"Błąd bazy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
