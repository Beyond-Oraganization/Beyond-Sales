"""Wyciąganie kontaktów z gotowego `PageData` (bez bs4): e-maile, telefony, profile firmowe w social media,
formularze kontaktowe.

Zasada: precyzja ponad liczność. Dekodujemy wyłącznie to, co strona sama publikuje (mailto, tekst, JSON-LD,
Cloudflare `data-cfemail`, zapis typu `info [at] firma.pl`). Bez JS, bez OCR, bez zgadywania adresów.
W v1 nie wyciągamy imion/nazwisk ani adresów pocztowych.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import quote, unquote, urlsplit

from perno.scraper.extract import FormInfo, PageData
from perno.scraper.urls import host_key

MAX_PER_TYPE = 20
MAX_VALUE_LEN = 255

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}(?![\w-])")
_AT_RE = re.compile(r"\s*[\[({]\s*(?:at|małpa|malpa|@)\s*[\])}]\s*", re.I)
_DOT_RE = re.compile(r"\s*[\[({]\s*(?:dot|kropka)\s*[\])}]\s*", re.I)

ASSET_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "bmp", "css", "js", "map", "woff", "woff2",
              "ttf", "eot", "otf", "pdf", "mp4", "mov", "webm", "mp3", "zip", "json", "xml", "html", "php"}
JUNK_DOMAINS = ("example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "yourdomain.pl",
                "yoursite.com", "yourcompany.com", "mysite.com", "mydomain.com", "test.com", "sentry.io",
                "wixpress.com", "schema.org", "w3.org", "wordpress.org", "gravatar.com", "googleapis.com",
                "sentry-next.wixpress.com", "email.com", "company.com", "website.com", "sampledomain.com",
                "twojadomena.pl")
JUNK_LOCALS = {"noreply", "no-reply", "donotreply", "do-not-reply", "do_not_reply", "mailer-daemon", "postmaster",
               "nobody", "bounce", "bounces", "unsubscribe"}
LEGAL_LOCALS = {"iod", "rodo", "dpo", "inspektor", "privacy", "gdpr", "abuse", "webmaster", "hostmaster", "security",
                "legal", "compliance", "daneosobowe", "ochronadanych", "prawny", "ido"}
SALES_LOCALS = {"sales", "sprzedaz", "handlowy", "handel", "oferty", "oferta", "zamowienia", "zamowienie",
                "rezerwacje", "rezerwacja", "booking", "bookings", "biznes", "business", "b2b", "partnerzy",
                "partner", "wspolpraca", "reservations", "orders"}
GENERAL_LOCALS = {"info", "biuro", "kontakt", "contact", "office", "hello", "hi", "mail", "email", "recepcja",
                  "sekretariat", "support", "pomoc", "obsluga", "bok", "help", "admin", "marketing", "pr",
                  "media", "press", "praca", "kariera", "hr", "jobs", "team", "zespol", "firma"}
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class Contact:
    typ: str  # email | telefon | social | formularz
    wartosc: str
    zrodlo_typ: str  # mailto | tekst | cfemail | jsonld | tel | link | forma
    w_domenie: bool = False
    rola: str = ""


# ---- e-maile -----------------------------------------------------------------

def decode_cfemail(hexa: str) -> Optional[str]:
    """Cloudflare e-mail protection: pierwszy bajt = klucz XOR pozostałych."""
    try:
        raw = bytes.fromhex(hexa)
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    key = raw[0]
    try:
        return bytes(byte ^ key for byte in raw[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def normalize_email(raw: str) -> Optional[str]:
    email = (raw or "").strip().strip("<>\"' ,;:").lower()
    if email.startswith("mailto:"):
        email = email[7:]
    if not EMAIL_RE.fullmatch(email):
        return None
    local, domain = email.rsplit("@", 1)
    if not local or ".." in local or ".." in domain:
        return None
    if local[0] in ".-_%+" or local[-1] in ".-_%+":
        return None
    if local.startswith(("u003", "u002", "x22", "x27")) or "%" in local:
        return None  # artefakty JSON/URL
    if _HEX32.match(local) or re.fullmatch(r"[0-9a-f]{24,}", local):
        return None
    if local in JUNK_LOCALS or local.replace(".", "").replace("-", "") in JUNK_LOCALS:
        return None
    tld = domain.rsplit(".", 1)[1]
    if tld in ASSET_TLDS:
        return None
    for junk in JUNK_DOMAINS:
        if domain == junk or domain.endswith("." + junk):
            return None
    if domain.startswith(("-", ".")) or "-." in domain or ".-" in domain:
        return None
    if re.search(r"@\d+x\.", email):  # logo@2x.png
        return None
    return email


def email_role(email: str) -> str:
    local = email.split("@", 1)[0]
    head = re.split(r"[._+-]", local, maxsplit=1)[0]
    for candidate in (local, head):
        if candidate in LEGAL_LOCALS:
            return "prawny"
    for candidate in (local, head):
        if candidate in SALES_LOCALS:
            return "sprzedaz"
    for candidate in (local, head):
        if candidate in GENERAL_LOCALS:
            return "ogolny"
    return "inny"


def emails_in_text(text: str) -> list:
    """Adresy z tekstu widocznego, także zapisane jako `jan [at] firma [dot] pl`. Kolejność wystąpienia, bez duplikatów."""
    prepared = _DOT_RE.sub(".", _AT_RE.sub("@", text or ""))
    found, seen = [], set()
    for match in EMAIL_RE.finditer(prepared):
        email = normalize_email(match.group(0))
        if email and email not in seen:
            seen.add(email)
            found.append(email)
    return found


def _emails_from_mailto(href: str) -> list:
    body = unquote(href[7:]).split("?", 1)[0]
    out = []
    for part in re.split(r"[,;]", body):
        match = EMAIL_RE.search(part)
        email = normalize_email(match.group(0)) if match else None
        if email:
            out.append(email)
    return out


def _jsonld_values(node, keys: tuple, depth: int = 0) -> Iterable[tuple]:
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("review", "reviews", "author", "comment", "reviewBody"):
                continue
            if key in keys:
                for item in (value if isinstance(value, list) else [value]):
                    if isinstance(item, str):
                        yield key, item
            elif isinstance(value, (dict, list)):
                yield from _jsonld_values(value, keys, depth + 1)
    elif isinstance(node, list):
        for item in node[:100]:
            yield from _jsonld_values(item, keys, depth + 1)


# ---- telefony ------------------------------------------------------------------

_LANDLINE = {"12", "13", "14", "15", "16", "17", "18", "22", "23", "24", "25", "29", "32", "33", "34", "41", "42",
             "43", "44", "46", "48", "52", "54", "55", "56", "58", "59", "61", "62", "63", "65", "67", "68", "71",
             "74", "75", "76", "77", "81", "82", "83", "84", "85", "86", "87", "89", "91", "94", "95"}
_MOBILE = {"45", "50", "51", "53", "57", "60", "66", "69", "72", "73", "78", "79", "88"}
_SPECIAL = {"80", "70"}  # infolinie 800/801, 70x
PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)(?:\+|00)?\d[\d \t\u00a0().\-]{6,19}\d(?![\d])")
_ID_CONTEXT_RE = re.compile(r"\b(?:nip|regon|krs|iban|kont[oa]|pesel|rachun\w*|account|vat|eori|swift|isbn|sku|nr\s*dow\w*)\b", re.I)
_MONEY_AFTER_RE = re.compile(r"^\s*(?:zł|zl|pln|eur|€|\$|usd|netto|brutto|szt|kg|m2|m²|km|%)", re.I)
_DATE_RE = re.compile(r"^\d{1,4}[.\-/]\d{1,2}[.\-/]\d{2,4}(?:\s+\d{1,2})?$")


def _degenerate(digits: str) -> bool:
    if len(set(digits)) <= 2:
        return True
    if re.search(r"(\d)\1{5,}", digits):
        return True
    return digits in ("123456789", "987654321", "012345678", "123456780", "1234567890")


def normalize_phone(raw: str, *, international: bool = False, trusted: bool = False) -> Optional[str]:
    """`trusted` = numer z `tel:` albo JSON-LD (strona sama go oznacza jako telefon)."""
    stripped = raw.strip()
    explicit = stripped.startswith("+") or stripped.startswith("00")
    digits = re.sub(r"\D", "", stripped)
    if stripped.startswith("00"):
        digits = digits[2:]
    if not 8 <= len(digits) <= 15 or _degenerate(digits):
        return None
    if explicit and digits.startswith("48") and len(digits) == 11:
        national = digits[2:]
    elif not explicit and len(digits) == 9:
        national = digits
    elif not explicit and len(digits) == 10 and digits.startswith("0") and digits[1:3] in _LANDLINE:
        national = digits[1:]  # 0 22 … (stary prefiks międzymiastowy)
    elif explicit and not digits.startswith("48"):
        if not (international or trusted) or not 8 <= len(digits) <= 15:
            return None
        return "+" + digits
    else:
        return None
    if national[:2] not in _LANDLINE | _MOBILE | _SPECIAL:
        return None
    if _degenerate(national):
        return None
    return "+48" + national


def phones_in_text(text: str, *, international: bool = False) -> list:
    found, seen = [], set()
    for match in PHONE_CANDIDATE_RE.finditer(text or ""):
        raw = match.group(0)
        if _DATE_RE.match(raw.strip()):
            continue
        before = text[max(0, match.start() - 24):match.start()]
        if _ID_CONTEXT_RE.search(before):
            continue
        if _MONEY_AFTER_RE.match(text[match.end():match.end() + 8]):
            continue
        number = normalize_phone(raw, international=international)
        if number and number not in seen:
            seen.add(number)
            found.append(number)
    return found


# ---- social ----------------------------------------------------------------------

_SOCIAL_HOSTS = {
    "facebook.com": "facebook", "fb.com": "facebook", "instagram.com": "instagram", "linkedin.com": "linkedin",
    "x.com": "x", "twitter.com": "x", "youtube.com": "youtube", "tiktok.com": "tiktok", "github.com": "github",
}
_FB_BAD = {"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login", "login.php", "profile.php",
           "people", "groups", "events", "watch", "hashtag", "policies", "privacy", "help", "ads", "business", "l.php",
           "permalink.php", "photo.php", "story.php", "photo", "photos", "video", "videos", "reel", "reels", "stories",
           "marketplace", "gaming", "public", "recover", "reg", "v2.0", "v3.0", "v18.0", "legal", "about", "home.php",
           "feeds", "flx", "search", "settings", "me", "report", "pages", "friends", "notes", "places", "fundraisers"}
_IG_BAD = {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "about", "legal", "web", "developer",
           "privacy", "directory", "challenge", "emails", "oauth", "share"}
_X_BAD = {"intent", "share", "home", "i", "search", "hashtag", "login", "settings", "tos", "privacy", "explore",
          "messages", "notifications", "compose", "who_to_follow", "widgets.js", "signup", "download", "about"}
_GH_BAD = {"topics", "features", "pricing", "about", "login", "join", "marketplace", "sponsors", "orgs", "apps",
           "collections", "trending", "events", "explore", "settings", "enterprise", "customer-stories", "readme", "search"}


def social_profile(url: str) -> Optional[tuple]:
    """(platforma, kanoniczny URL) tylko dla profili firmowych; share/intent/plugin/profile osobisty → None."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile.", "pl-pl.", "en-gb.", "business."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    platform = _SOCIAL_HOSTS.get(host)
    if not platform:
        return None
    segments = [unquote(seg) for seg in parts.path.split("/") if seg]
    if not segments:
        return None
    first = segments[0].lower()
    if platform == "facebook":
        if first in _FB_BAD or first.endswith(".php"):
            return None
        keep = segments[:1]
    elif platform == "instagram":
        if first in _IG_BAD:
            return None
        keep = segments[:1]
    elif platform == "linkedin":
        if first not in ("company", "school", "showcase") or len(segments) < 2:
            return None
        keep = segments[:2]
    elif platform == "x":
        if first in _X_BAD:
            return None
        keep = segments[:1]
    elif platform == "youtube":
        if first.startswith("@"):
            keep = segments[:1]
        elif first in ("c", "channel", "user") and len(segments) >= 2:
            keep = segments[:2]
        else:
            return None
    elif platform == "tiktok":
        if not first.startswith("@"):
            return None
        keep = segments[:1]
    else:  # github
        if first in _GH_BAD:
            return None
        keep = segments[:1]
    keep = [quote(seg, safe="@.-_+") for seg in keep]  # wynik zawsze ASCII
    if any(not re.fullmatch(r"[A-Za-z0-9.@\-_+%]{1,150}", seg) for seg in keep):
        return None
    canonical_host = {"x": "x.com", "facebook": "facebook.com"}.get(platform, host)
    return platform, "https://" + canonical_host + "/" + "/".join(keep)


# ---- formularze ----------------------------------------------------------------

_FORM_WIDGETS = ("wpcf7", "elementor-form", "gform_", "gform-", "wpforms", "frm_forms", "hs-form", "contact-form",
                 "contactform", "nf-form", "forminator", "fluentform", "caldera", "cf7", "form-contact", "kontakt")
_EMBED_FORM_HOSTS = ("typeform.com", "tally.so", "forms.gle", "jotform.com", "airtable.com", "formspree.io",
                     "form.jotform.com", "hsforms.com", "hsforms.net")
_MESSAGE_FIELD_RE = re.compile(r"message|wiadomo|tre[sś][cć]|komentarz|pytanie|your-message|msg|zapytanie|opis", re.I)


def is_contact_form(form: FormInfo) -> bool:
    if form.is_search or form.has_password:
        return False
    if "comment" in form.ident or "wp-comments-post" in form.action:
        return False
    widget = any(marker in form.ident for marker in _FORM_WIDGETS)
    has_message = form.has_textarea or any(_MESSAGE_FIELD_RE.search(name + " " + placeholder)
                                          for _tag, _type, name, placeholder in form.fields)
    if form.has_email and has_message:
        return True
    return widget and form.has_email  # widget bez pola wiadomości (np. krótki formularz zapytania)


def _embed_has_form(embeds: list) -> bool:
    for src in embeds:
        host = (urlsplit(src).hostname or "").lower()
        path = urlsplit(src).path.lower()
        if any(host == h or host.endswith("." + h) for h in _EMBED_FORM_HOSTS):
            return True
        if host.endswith("google.com") and "/forms" in path:
            return True
    return False


# ---- całość ----------------------------------------------------------------------

_RANK = {"mailto": 0, "cfemail": 1, "tel": 0, "jsonld": 2, "link": 1, "forma": 0, "tekst": 3}


def _in_domain(email: str, hosts: Iterable[Optional[str]]) -> bool:
    domain = host_key(email.rsplit("@", 1)[1])
    if not domain:
        return False
    return any(host and (domain == host or domain.endswith("." + host)) for host in hosts)


def extract_contacts(data: PageData, page_url: str, company_host: str, extra_hosts: Iterable[str] = (),
                     *, international: bool = False) -> list:
    """Kontakty z jednej strony. `company_host` = `host_key` domeny firmy; `extra_hosts` np. `domena_docelowa`."""
    hosts = [host_key(company_host)] + [host_key(h) for h in extra_hosts if h]
    best: dict = {}

    def add(typ: str, value: str, source: str, **extra) -> None:
        key = (typ, value)
        current = best.get(key)
        if current is None or _RANK[source] < _RANK[current.zrodlo_typ]:
            best[key] = Contact(typ, value, source, **extra)

    def add_email(email: str, source: str) -> None:
        add("email", email, source, w_domenie=_in_domain(email, hosts), rola=email_role(email))

    for href in data.mailtos:
        for email in _emails_from_mailto(href):
            add_email(email, "mailto")
    for hexa in data.cfemails:
        decoded = decode_cfemail(hexa)
        email = normalize_email(decoded) if decoded else None
        if email:
            add_email(email, "cfemail")
    for key, value in _jsonld_values(data.jsonld, ("email",)):
        email = normalize_email(value)
        if email:
            add_email(email, "jsonld")
    for email in emails_in_text(data.text):
        add_email(email, "tekst")

    for href in data.tels:
        number = normalize_phone(unquote(href[4:]).split(";", 1)[0], international=international, trusted=True)
        if number:
            add("telefon", number, "tel")
    for key, value in _jsonld_values(data.jsonld, ("telephone",)):
        number = normalize_phone(value, international=international, trusted=True)
        if number:
            add("telefon", number, "jsonld")
    for number in phones_in_text(data.text, international=international):
        add("telefon", number, "tekst")

    for link in data.links:
        profile = social_profile(link.href)
        if profile:
            add("social", profile[1], "link")
    for key, value in _jsonld_values(data.jsonld, ("sameAs",)):
        profile = social_profile(value)
        if profile:
            add("social", profile[1], "jsonld")

    if any(is_contact_form(form) for form in data.forms) or _embed_has_form(data.embeds):
        add("formularz", page_url, "forma")

    out: list = []
    counts: dict = {}
    for contact in best.values():
        if len(contact.wartosc) > MAX_VALUE_LEN:  # kolumna kontakty.wartosc ma 255 znaków
            continue
        counts[contact.typ] = counts.get(contact.typ, 0) + 1
        if counts[contact.typ] <= MAX_PER_TYPE:
            out.append(contact)
    return out
