"""Ekstrakcja danych Eventy: tablica JS z Maile_GTM_Eventy/index.html
+ uzupełnianie e-maili i firm z dossier w Firmy/Eventy/*.md.
"""

from __future__ import annotations

import glob
import os
import re

ARRAY_RE = re.compile(r"const companies = \[(.*?)\n\];", re.S)
KARTA_RE = re.compile(r"##\s*0\.\s*Karta firmy(.*?)(?:\n##\s|\Z)", re.S)
H1_RE = re.compile(r"^#\s*\d+\.\s*(.+?)\s*—\s*analiza firmy\s*$", re.M)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s|]+")
DOSSIER_NAME_RE = re.compile(r"^(\d{3})_(.+)\.md$")

_PL_MAP = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)

ORG_KEYWORDS = [
    "sp. z o.o.", "s.a.", "sp.j.", "fundacja", "centrum", "akademia", "klub",
    "grupa", "event", "studio", "pracownia", "poligon", "warsztat",
    "organizator", "stowarzyszenie", "szkoła", "szkola", "agency", "tech",
    "group",
]


def _unescape_js(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _normalize(text: str) -> str:
    text = text.translate(_PL_MAP).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_name(name: str) -> tuple[str, str]:
    """Rozdziela 'Nazwa (dopisek)' na (nazwa_wyświetlana, dopisek). Brak
    nawiasu -> dopisek pusty."""
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", name.strip())
    if not m:
        return name.strip(), ""
    return m.group(1).strip(), m.group(2).strip()


def guess_contact_person(suffix: str) -> tuple[str, str]:
    """Heurystyka: czy dopisek w nawiasie to osoba kontaktowa, czy coś
    innego (nazwa prawna, domena, opis)? Zwraca (guess, confidence), gdzie
    confidence in {"high", "low", "none"}. To tylko podpowiedź do ręcznego
    sprawdzenia, nie pewnik — patrz `contact_person_confidence` w danych."""
    s = suffix.strip()
    if not s:
        return "", "none"
    low = s.lower()
    if "@" in s or "/" in s or "http" in low or re.search(r"\.\w{2,4}(?:[\s)]|$)", s):
        return "", "none"
    if any(kw in low for kw in ORG_KEYWORDS):
        return "", "none"
    words = [w for w in re.sub(r"[,&]| i ", " ", s).split() if w]
    if 1 <= len(words) <= 4 and all(w[:1].isupper() for w in words) and not any(c.isdigit() for c in s):
        return s, "high"
    return s, "low"


def parse_js_companies(html_text: str) -> list[dict]:
    """Parsuje tablicę `const companies = [...]` z index.html. Własny
    parser po głębokości nawiasów {} zamiast jednego regexa na całość —
    wiele wartości tekstowych zawiera przecinki, co rozjeżdża naiwny split."""
    match = ARRAY_RE.search(html_text)
    if not match:
        raise ValueError("Nie znaleziono 'const companies = [...]' w index.html")
    block = match.group(1)

    objs, depth, buf = [], 0, ""
    for ch in block:
        if ch == "{":
            depth += 1
        if depth > 0:
            buf += ch
        if ch == "}":
            depth -= 1
            if depth == 0:
                objs.append(buf)
                buf = ""

    def field(obj_text: str, key: str, default: str = "") -> str:
        m = re.search(key + r'\s*:\s*"((?:[^"\\]|\\.)*)"', obj_text)
        return _unescape_js(m.group(1)) if m else default

    results = []
    for obj_text in objs:
        results.append({
            "name": field(obj_text, "name"),
            "website": field(obj_text, "website"),
            "email": field(obj_text, "email"),
            "tag": field(obj_text, "tag"),
            "mode": field(obj_text, "mode", "Panstwo"),
            "subject": field(obj_text, "subject"),
            "obs": field(obj_text, "obs"),
            "pitch": field(obj_text, "pitch"),
            "channel_summary": field(obj_text, "channelSummary"),
            "already_customer": bool(re.search(r"alreadyCustomer\s*:\s*true", obj_text)),
            "note": field(obj_text, "note"),
        })
    return results


def list_dossiers(dossiers_dir: str) -> dict:
    """{row_number: {"path", "slug_norm"}} dla plików NNN_*.md (pomija
    00_METODOLOGIA.md i 00_PODSUMOWANIE.md, które mają tylko 2 cyfry)."""
    out = {}
    for path in glob.glob(os.path.join(dossiers_dir, "[0-9][0-9][0-9]_*.md")):
        m = DOSSIER_NAME_RE.match(os.path.basename(path))
        if not m:
            continue
        row_number = int(m.group(1))
        slug = m.group(2).replace("_", " ").replace("-", " ")
        out[row_number] = {"path": path, "slug_norm": _normalize(slug)}
    return out


def match_dossier(company_name: str, dossiers: dict) -> int | None:
    """Dopasowuje nazwę firmy z tablicy JS do numeru dossier po znormalizowanej
    nazwie (obustronne 'containment'), nie po pozycji w tablicy — nazwy w
    dossier i w arkuszu czasem się różnią dla tej samej firmy."""
    display_name, _ = split_name(company_name)
    full_norm = _normalize(company_name)
    primary_norm = _normalize(display_name)

    candidates = []
    for row_number, info in dossiers.items():
        slug = info["slug_norm"]
        if slug == full_norm or slug == primary_norm:
            candidates.append((0, row_number))
        elif slug.startswith(primary_norm) or primary_norm.startswith(slug):
            candidates.append((1, row_number))
        elif primary_norm in slug or slug in full_norm:
            candidates.append((2, row_number))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def read_dossier_text(path: str) -> str:
    """Treść pliku dossier (UTF-8)."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_company_card_email(dossier_text: str) -> str:
    """E-mail wyłącznie z wiersza 'Kontakt publiczny' w tabeli Karta firmy —
    NIGDY z całego dokumentu (dossier bywają zawierają w prozie przykłady
    danych-zaślepek ze stron klientów, np. jan.kowalski@gmail.com opisany
    jako placeholder, nie prawdziwy kontakt)."""
    section = KARTA_RE.search(dossier_text)
    if not section:
        return ""
    for line in section.group(1).splitlines():
        if line.strip().startswith("|") and "kontakt" in line.lower():
            found = EMAIL_RE.findall(line)
            if found:
                return found[0]
    return ""


def extract_company_card_website(dossier_text: str) -> str:
    """Adres strony z wiersza „Strona” w tabeli Karta firmy (pusty, gdy go brak)."""
    section = KARTA_RE.search(dossier_text)
    if not section:
        return ""
    for line in section.group(1).splitlines():
        if line.strip().startswith("|") and "strona" in line.lower():
            found = URL_RE.findall(line)
            if found:
                return found[0].rstrip(").,;`'\"")
    return ""


def extract_h1_name(dossier_text: str) -> str:
    """Nazwa firmy z nagłówka dossier („# N. Nazwa — analiza firmy”)."""
    m = H1_RE.search(dossier_text)
    return m.group(1).strip() if m else ""
