"""Normalizacja adresów, zakres (ta sama domena), reguły pułapek i priorytety.

Czyste funkcje (bez sieci i bez bazy). Wynik `normalize_url` jest zawsze czysto
ASCII (kolumny `url`/`url_hash` w bazie są ASCII) albo `None`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlencode, urljoin, urlsplit

MAX_URL_LENGTH = 2000
MAX_QUERY_PARAMS = 8

TRACKING_PARAMS = frozenset({
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_ga", "_gl", "yclid", "igshid",
    "replytocom", "share", "phpsessid", "sid", "jsessionid",
})
BLOCKED_QUERY_NAMES = frozenset({
    "s", "q", "search", "orderby", "order", "sort", "min_price", "max_price", "add-to-cart",
    "wc-ajax", "ajax", "action", "print", "attachment_id", "rest_route", "feed", "ical",
    "outlook-ical", "tribe-bar-date", "month", "year", "date",
})
BLOCKED_QUERY_PREFIXES = ("filter", "pa_")
IDENTITY_PARAMS = frozenset({"p", "page_id", "id", "article", "post", "product", "product_id", "pid", "nid", "node"})
PAGINATION_PARAMS = frozenset({"paged", "page", "strona", "pg"})

BLOCKED_PATH_PREFIXES = ("/wp-json/", "/wp-admin/", "/wp-content/", "/wp-includes/", "/cdn-cgi/")
BLOCKED_PATHS = frozenset({"/wp-login.php", "/xmlrpc.php"})
BLOCKED_SEGMENTS = frozenset({
    "feed", "trackback", "tag", "tagi", "etykiety", "author", "autor",
    "koszyk", "cart", "zamowienie", "checkout", "moje-konto", "my-account", "logowanie", "login",
    "rejestracja", "register", "wishlist", "porownaj", "compare", "szukaj",
})
CATEGORY_SEGMENTS = frozenset({"category", "kategoria", "kategorie"})
DATE_ARCHIVE_RE = re.compile(r"^/(19|20)\d{2}(/\d{1,2}){0,2}/?$")
PAGE_N_RE = re.compile(r"/page/\d+/?$", re.I)

SKIP_EXTENSIONS = frozenset(
    ".jpg .jpeg .png .gif .webp .svg .ico .bmp .tif .tiff .avif .pdf .doc .docx .xls .xlsx .ppt .pptx "
    ".odt .ods .zip .rar .7z .gz .tgz .tar .bz2 .mp3 .mp4 .avi .mov .wmv .mkv .webm .flac .wav .ogg "
    ".css .js .json .xml .woff .woff2 .ttf .eot .otf .exe .dmg .apk .csv .rss .atom".split()
)

PLATFORM_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "youtube.com", "youtu.be", "google.com", "goo.gl", "tiktok.com", "booksy.com", "pinterest.com",
    "t.me", "wa.me",
)

# (klasa, premia priorytetu, wzorzec w ścieżce)
PAGE_CLASSES = (
    ("kontakt", 80, re.compile(r"kontakt|contact|skontaktuj|napisz-do-nas|formularz")),
    ("o_nas", 70, re.compile(r"o-nas|o-firmie|about|firma|zespol|team|zarzad|kariera|career|jobs")),
    ("prawne", 60, re.compile(r"impressum|imprint|regulamin|polityka-prywatnosci|privacy|rodo|terms|stopka|dane-firmy")),
    ("oferta", 40, re.compile(r"oferta|uslugi|services|produkty|products|cennik|pricing|realizacje|portfolio|referencje|klienci|case")),
)
CLASS_BONUS = {name: bonus for name, bonus, _ in PAGE_CLASSES}
SITEMAP_BASE_PRIORITY = 150
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_SAFE_SEGMENT = "!$&'()*+,;=:@-._~"


# ---- host / zakres -----------------------------------------------------

def _ascii_host(host: str) -> Optional[str]:
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host if _HOST_RE.match(host) else None


def host_key(host: str) -> Optional[str]:
    """Klucz domeny: małe litery, IDNA, bez końcowej kropki i bez wiodącego `www.`."""
    host = _ascii_host(host)
    if host is None:
        return None
    return host[4:] if host.startswith("www.") and len(host) > 4 else host


def site_key(url: str) -> Optional[str]:
    """host_key z ewentualnym niestandardowym portem (zakres = jedna witryna)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    key = host_key(parts.hostname or "")
    if key is None:
        return None
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    return key if port in (None, default) else f"{key}:{port}"


def is_platform(host: str) -> bool:
    key = host_key(host)
    return bool(key) and any(key == p or key.endswith("." + p) for p in PLATFORM_HOSTS)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("ascii")).hexdigest()


# ---- normalizacja ------------------------------------------------------

def _norm_path(path: str, has_query: bool) -> str:
    path = re.sub(r";jsessionid=[^/?]*", "", path or "", flags=re.I)
    if not path.startswith("/"):
        path = "/" + path
    trailing = path.endswith("/")
    out: list = []
    for segment in path.split("/")[1:]:
        if segment == "" or segment == ".":
            continue
        if segment == "..":
            if out:
                out.pop()
            trailing = True
            continue
        out.append(quote(unquote_to_bytes(segment), safe=_SAFE_SEGMENT))
    result = "/" + "/".join(out)
    if trailing and out:
        result += "/"
    if not has_query:
        result = re.sub(r"/(?:index\.(?:html?|php)|default\.aspx)$", "/", result, flags=re.I)
    return result


def _norm_query(query: str) -> Optional[str]:
    if not query:
        return ""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=50)
    except ValueError:
        return None
    kept = [(k, v) for k, v in pairs if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
    kept.sort()
    return urlencode(kept, quote_via=quote, safe="")


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    if not url or len(url) > 4 * MAX_URL_LENGTH:
        return None
    url = url.strip()
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or parts.username or parts.password:
        return None
    host = _ascii_host(parts.hostname or "")
    if host is None:
        return None
    netloc = host if port in (None, 80 if scheme == "http" else 443) else f"{host}:{port}"
    query = _norm_query(parts.query)
    if query is None:
        return None
    path = _norm_path(parts.path, has_query=bool(query))
    result = f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")
    return result if len(result) <= MAX_URL_LENGTH and result.isascii() else None


def validate_start_url(raw: str) -> tuple:
    """(znormalizowany URL, "") albo (None, kod_przyczyny) — kody jak w statuses.PRZYCZYNY."""
    raw = (raw or "").strip()
    if not raw:
        return None, "brak_www"
    if "://" not in raw:
        raw = "https://" + raw
    normalized = normalize_url(raw)
    if normalized is None:
        return None, "zly_url"
    host = urlsplit(normalized).hostname or ""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None, "zly_url"
    except ValueError:
        pass
    if "." not in host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return None, "zly_url"
    if is_platform(host):
        return None, "platforma"
    return normalized, ""


# ---- reguły statyczne (bez stanu) ---------------------------------------

def _query_pairs(query: str) -> list:
    return parse_qsl(query, keep_blank_values=True) if query else []


def page_class(path: str) -> str:
    """Klasa „strony kluczowej" (kontakt/o_nas/prawne/oferta) albo ''."""
    lowered = path.lower()
    for name, _, pattern in PAGE_CLASSES:
        if pattern.search(lowered):
            return name
    return ""


def static_reject(url: str) -> Optional[str]:
    """Kod przyczyny odrzucenia URL-a (albo None). Nie zależy od historii crawla."""
    parts = urlsplit(url)
    path = parts.path.lower()
    segments = [s for s in path.split("/") if s]
    if os.path.splitext(segments[-1])[1] in SKIP_EXTENSIONS if segments else False:
        return "rozszerzenie"
    if path in BLOCKED_PATHS or path.startswith(BLOCKED_PATH_PREFIXES):
        return "sciezka_zablokowana"
    if any(s in BLOCKED_SEGMENTS for s in segments):
        return "sciezka_zablokowana"
    if segments and segments[-1] == "embed":
        return "sciezka_zablokowana"
    if DATE_ARCHIVE_RE.match(path):
        return "archiwum_dat"
    if len(segments) > 10:
        return "zbyt_glebokie"
    counts: dict = defaultdict(int)
    for s in segments:
        counts[s] += 1
    if any(n >= 3 for n in counts.values()):
        return "petla_segmentow"
    pairs = _query_pairs(parts.query)
    if len(pairs) > MAX_QUERY_PARAMS:
        return "za_duzo_parametrow"
    for name, _ in pairs:
        lowered = name.lower()
        if lowered in BLOCKED_QUERY_NAMES or lowered.startswith(BLOCKED_QUERY_PREFIXES):
            return "zapytanie_zablokowane"
    return None


# ---- reguły ze stanem (na jedną firmę) -----------------------------------

def _parent_dir(path: str) -> str:
    return path[: path.rfind("/") + 1] or "/"


def pattern_of(url: str) -> str:
    parts = urlsplit(url)
    names = sorted({k.lower() for k, _ in _query_pairs(parts.query)})
    if names:
        return f"q:{parts.path}?{','.join(names)}"
    return f"d:{_parent_dir(parts.path)}"


class TrapGuard:
    """Limity wzorców/katalogów/paginacji dla jednej firmy. `admit` rejestruje URL."""

    DIR_CAP = 40
    ROOT_DIR_CAP = 100  # płaskie strony trzymają wszystko w katalogu głównym
    DIR_KEY_CAP = 10
    CATEGORY_CAP = 10
    PAGINATION_CAP = 5
    PATTERN_CAP = 5
    IDENTITY_CAP = 100
    DEAD_WINDOW = 8

    def __init__(self):
        self.dir_count: dict = defaultdict(int)
        self.dir_key_count: dict = defaultdict(int)
        self.category_count = 0
        self.pagination: dict = defaultdict(int)
        self.pattern_variants: dict = defaultdict(set)
        self.identity_values: dict = defaultdict(set)
        self.recent: dict = {}
        self.dead: set = set()

    def admit(self, url: str) -> tuple:
        reason = static_reject(url)
        if reason:
            return False, reason
        parts = urlsplit(url)
        path = parts.path
        pairs = _query_pairs(parts.query)
        pattern = pattern_of(url)
        if pattern in self.dead:
            return False, "martwy_wzorzec"

        segments = [s.lower() for s in path.split("/") if s]
        is_category = any(s in CATEGORY_SEGMENTS for s in segments)
        if is_category and self.category_count >= self.CATEGORY_CAP:
            return False, "limit_kategorii"

        pagination_key = None
        page_match = PAGE_N_RE.search(path)
        page_params = [k for k, _ in pairs if k.lower() in PAGINATION_PARAMS]
        if page_match or page_params:
            listing_path = PAGE_N_RE.sub("/", path)
            rest = sorted((k, v) for k, v in pairs if k.lower() not in PAGINATION_PARAMS)
            pagination_key = (listing_path, tuple(rest))
            if self.pagination[pagination_key] >= self.PAGINATION_CAP:
                return False, "limit_paginacji"

        identity = [(k.lower(), v) for k, v in pairs if k.lower() in IDENTITY_PARAMS]
        other_names = frozenset(k.lower() for k, _ in pairs if k.lower() not in IDENTITY_PARAMS)
        new_identity = []
        for name, value in identity:
            values = self.identity_values[name]
            if value not in values and len(values) >= self.IDENTITY_CAP:
                return False, "limit_identyfikatorow"
            new_identity.append((name, value))
        variant_key = None
        if other_names and not identity:
            variant_key = (path, other_names)
            variants = self.pattern_variants[variant_key]
            if parts.query not in variants and len(variants) >= self.PATTERN_CAP:
                return False, "limit_wzorca"

        parent = _parent_dir(path)
        is_key = bool(page_class(path))
        # Adresy z parametrem tożsamości (`/?p=123`) nie należą do katalogu — rządzi nimi własny limit.
        uses_directory = not identity
        if uses_directory:
            if is_key:
                if self.dir_key_count[parent] >= self.DIR_KEY_CAP:
                    return False, "limit_katalogu"
            else:
                cap = self.ROOT_DIR_CAP if parent == "/" else self.DIR_CAP
                if self.dir_count[parent] >= cap:
                    return False, "limit_katalogu"

        # wszystko przeszło — zatwierdź liczniki
        if uses_directory:
            if is_key:
                self.dir_key_count[parent] += 1
            else:
                self.dir_count[parent] += 1
        if is_category:
            self.category_count += 1
        if pagination_key is not None:
            self.pagination[pagination_key] += 1
        for name, value in new_identity:
            self.identity_values[name].add(value)
        if variant_key is not None:
            self.pattern_variants[variant_key].add(parts.query)
        return True, ""

    def record(self, url: str, useful: bool) -> None:
        """Zgłoś wynik pobrania; wzorzec, z którego 8 ostatnich stron było duplikatami/pustych, zostaje wyłączony."""
        pattern = pattern_of(url)
        window = self.recent.setdefault(pattern, deque(maxlen=self.DEAD_WINDOW))
        window.append(bool(useful))
        if len(window) == self.DEAD_WINDOW and not any(window):
            self.dead.add(pattern)


# ---- priorytet ---------------------------------------------------------

def priority(url: str, depth: int, *, in_nav: bool = False, anchor_text: str = "",
             from_sitemap: bool = False, start_prefix: str = "/") -> int:
    """Mniejsza wartość = wcześniej. Strona startowa ma priorytet 0 nadawany przez crawlera."""
    path = urlsplit(url).path
    segments = len([s for s in path.split("/") if s])
    cls = page_class(path)
    bonus = CLASS_BONUS.get(cls, 0)
    if in_nav:
        bonus += 10
    anchor_cls = page_class(anchor_text.lower().replace(" ", "-")) if anchor_text else ""
    if anchor_cls:
        bonus += 10
    if start_prefix not in ("", "/") and path.startswith(start_prefix):
        bonus += 20
    base = SITEMAP_BASE_PRIORITY if from_sitemap else depth * 100
    return base + 5 * segments - bonus
