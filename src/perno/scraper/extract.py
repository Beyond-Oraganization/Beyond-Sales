"""Jedno parsowanie HTML → `PageData` (tekst, metadane, linki, surowe sygnały kontaktowe).

`bs4` jest importowany leniwie (`parse_html`), żeby brak pakietu nigdy nie zablokował panelu wysyłki
(import `perno.web.pages` nie może go ciągnąć). Moduł nie interpretuje kontaktów — robi to `contacts.py`
na gotowym `PageData`, więc ta logika testuje się bez bs4.
"""

from __future__ import annotations

import html as html_lib
import importlib.util
import json
import re
import warnings
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

MAX_TEXT_CHARS = 1_000_000
MAX_LINKS_PER_PAGE = 5000
MAX_JSONLD_BLOCKS = 20
MAX_JSONLD_BYTES = 200_000
MAX_TITLE = 500
MAX_DESCRIPTION = 1000
MAX_ANCHOR = 200

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "head", "object", "embed", "canvas")
_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "h1", "h2", "h3", "h4", "h5", "h6",
               "section", "article", "header", "footer", "nav", "aside", "main", "form", "blockquote",
               "pre", "address", "dd", "dt", "dl", "figure", "figcaption", "fieldset", "details", "summary")
_SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "sms:", "whatsapp:", "skype:", "callto:", "fax:", "#")
_WS_RE = re.compile(r"[ \t\r\f\v ​﻿]+")
_UA_DIRECTIVES = {"noindex", "nofollow", "none", "noarchive", "nosnippet", "notranslate", "noimageindex",
                  "unavailable_after", "max-snippet", "max-image-preview", "max-video-preview", "indexifembedded",
                  "all", "index", "follow", "nocache"}
_OUR_TOKENS = ("*", "pernocontextbot", "perno")
_BS4 = None


@dataclass
class Link:
    href: str
    text: str = ""
    where: str = "body"  # nav | footer | header | body
    nofollow: bool = False


@dataclass
class FormInfo:
    action: str = ""
    ident: str = ""  # małe litery: id, class, name, role
    fields: list = field(default_factory=list)  # (tag, type, name, placeholder) — małymi literami
    has_textarea: bool = False
    has_password: bool = False
    has_email: bool = False
    is_search: bool = False


@dataclass
class PageData:
    title: str = ""
    description: str = ""
    canonical: str = ""
    lang: str = ""
    meta_robots: frozenset = frozenset()
    text: str = ""
    links: list = field(default_factory=list)
    mailtos: list = field(default_factory=list)  # surowe href mailto:
    tels: list = field(default_factory=list)  # surowe href tel:
    cfemails: list = field(default_factory=list)  # hex z data-cfemail / email-protection#
    forms: list = field(default_factory=list)
    embeds: list = field(default_factory=list)  # src iframe (przed usunięciem)
    jsonld: list = field(default_factory=list)  # obiekty JSON-LD (spłaszczone @graph)
    truncated: bool = False
    error: str = ""


def bs4_available() -> bool:
    return importlib.util.find_spec("bs4") is not None


def _bs4():
    global _BS4
    if _BS4 is None:
        import bs4  # leniwy import: opcjonalna zależność skrapera
        for name in ("MarkupResemblesLocatorWarning", "XMLParsedAsHTMLWarning"):
            category = getattr(bs4, name, None)
            if category is not None:
                warnings.filterwarnings("ignore", category=category)  # raz, globalnie — catch_warnings nie jest wątkowo-bezpieczne
        _BS4 = bs4
    return _BS4


def collapse(value: Optional[str], limit: int) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\n", " ")).strip()[:limit]


def parse_html(html: str, base_url: str) -> PageData:
    bs4 = _bs4()
    data = PageData()
    try:
        soup = bs4.BeautifulSoup(html, "html.parser")
    except RecursionError:
        data.error = "zbyt_gleboki_dom"
        return data
    except Exception as exc:  # noqa: BLE001 — parser nie może zabić crawla firmy
        data.error = f"parser: {type(exc).__name__}"
        return data
    try:
        _fill(soup, base_url, data, html)
    except RecursionError:
        data.error = data.error or "zbyt_gleboki_dom"
    return data


def _fill(soup, base_url: str, data: PageData, raw_html: str = "") -> None:
    html_tag = soup.find("html")
    if html_tag is not None and html_tag.get("lang"):
        data.lang = collapse(str(html_tag.get("lang")), 20).lower()

    base = base_url
    base_tag = soup.find("base", href=True)
    if base_tag is not None:
        joined = urljoin(base_url, str(base_tag["href"]).strip())
        if joined.lower().startswith(("http://", "https://")):
            base = joined

    data.jsonld = _read_jsonld(soup)
    data.title = _title(soup, raw_html)
    data.description = _meta_content(soup, ("description", "og:description"))
    canonical = soup.find("link", rel=lambda value: value and "canonical" in (value if isinstance(value, list) else [value]))
    if canonical is not None and canonical.get("href"):
        data.canonical = urljoin(base, str(canonical["href"]).strip())[:2000]
    robots = _meta_content(soup, ("robots",), lower=True)
    data.meta_robots = frozenset(token.strip() for token in re.split(r"[,\s]+", robots) if token.strip())

    for tag in soup.find_all("iframe", src=True):
        if len(data.embeds) < 20:
            data.embeds.append(urljoin(base, str(tag["src"]).strip())[:500])

    data.forms = [_form_info(form, base) for form in soup.find_all("form")[:50]]

    seen = 0
    for anchor in soup.find_all(["a", "area"], href=True):
        href = str(anchor["href"]).strip()
        lowered = href.lower()
        if lowered.startswith("mailto:"):
            data.mailtos.append(href[:500])
            continue
        if lowered.startswith("tel:"):
            data.tels.append(href[:100])
            continue
        if "/cdn-cgi/l/email-protection#" in lowered:
            hexa = href.split("#", 1)[1]
            if re.fullmatch(r"[0-9a-fA-F]{4,200}", hexa or ""):
                data.cfemails.append(hexa)
            continue
        if not href or lowered.startswith(_SKIP_SCHEMES):
            continue
        if seen >= MAX_LINKS_PER_PAGE:
            data.truncated = True
            break
        seen += 1
        rel = anchor.get("rel") or []
        rel = rel if isinstance(rel, list) else str(rel).split()
        data.links.append(Link(href=urljoin(base, href)[:2000], text=collapse(anchor.get_text(" ", strip=True), MAX_ANCHOR),
                               where=_location(anchor), nofollow="nofollow" in [item.lower() for item in rel]))
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        hexa = str(tag.get("data-cfemail") or "")
        if re.fullmatch(r"[0-9a-fA-F]{4,200}", hexa):
            data.cfemails.append(hexa)

    # Tekst: dopiero po zebraniu sygnałów (usuwamy script/iframe itd.), bloki oddzielamy nowymi liniami,
    # a elementy liniowe sklejamy — "<span>info</span>@<span>firma.pl</span>" musi zostać jednym adresem.
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    raw = soup.get_text("")
    lines = (_WS_RE.sub(" ", line).strip() for line in raw.split("\n"))
    text = "\n".join(line for line in lines if line)
    if len(text) > MAX_TEXT_CHARS:
        text, data.truncated = text[:MAX_TEXT_CHARS], True
    data.text = text


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)


def _title(soup, raw_html: str = "") -> str:
    tag = soup.find("title")
    if tag is not None and tag.find(True) is None:
        value = collapse(tag.get_text(" "), MAX_TITLE)
    else:
        # `<title>` to w przeglądarkach zwykły tekst (RCDATA), a html.parser potrafi w nim zobaczyć elementy —
        # wtedy czytamy surowy tekst między znacznikami, tak jak przeglądarka.
        match = _TITLE_RE.search(raw_html or "")
        value = collapse(html_lib.unescape(match.group(1)), MAX_TITLE) if match else ""
    return value or _meta_content(soup, ("og:title",))[:MAX_TITLE]


def _meta_content(soup, names: tuple, lower: bool = False) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"name": re.compile(f"^{re.escape(name)}$", re.I)}) or \
              soup.find("meta", attrs={"property": re.compile(f"^{re.escape(name)}$", re.I)})
        if tag is not None and tag.get("content"):
            value = collapse(str(tag["content"]), MAX_DESCRIPTION)
            return value.lower() if lower else value
    return ""


def _location(anchor) -> str:
    depth = 0
    for parent in anchor.parents:
        depth += 1
        if depth > 12:
            break
        name = getattr(parent, "name", "") or ""
        if name == "nav":
            return "nav"
        if name == "footer":
            return "footer"
        if name == "header":
            return "header"
        attrs = getattr(parent, "attrs", None) or {}
        role = str(attrs.get("role") or "").lower()
        if role == "navigation":
            return "nav"
        if role == "contentinfo":
            return "footer"
        if role == "banner":
            return "header"
        classes = attrs.get("class") or []
        tokens = [str(item).lower() for item in (classes if isinstance(classes, list) else [classes])]
        tokens.append(str(attrs.get("id") or "").lower())
        for token in tokens:
            if "footer" in token:
                return "footer"
            if token in ("nav", "navbar", "navigation", "menu", "main-menu", "primary-menu", "site-nav") or token.startswith(("nav-", "menu-main")):
                return "nav"
    return "body"


def _form_info(form, base: str) -> FormInfo:
    attrs = form.attrs
    classes = attrs.get("class") or []
    classes = classes if isinstance(classes, list) else [classes]
    ident = " ".join([str(attrs.get("id") or ""), " ".join(str(item) for item in classes),
                      str(attrs.get("name") or ""), str(attrs.get("role") or "")]).lower()
    info = FormInfo(action=urljoin(base, str(attrs.get("action") or ""))[:500] if attrs.get("action") else "", ident=ident)
    info.is_search = "search" in ident or str(attrs.get("role") or "").lower() == "search"
    for control in form.find_all(["input", "textarea", "select"])[:60]:
        tag = control.name
        ctype = str(control.get("type") or ("textarea" if tag == "textarea" else "text")).lower()
        name = str(control.get("name") or control.get("id") or "").lower()
        placeholder = str(control.get("placeholder") or "").lower()
        info.fields.append((tag, ctype, name, placeholder))
        if tag == "textarea":
            info.has_textarea = True
        if ctype == "password":
            info.has_password = True
        if ctype == "search":
            info.is_search = True
        if ctype == "email" or "email" in name or "e-mail" in name or "e-mail" in placeholder or "email" in placeholder:
            info.has_email = True
    return info


# ---- JSON-LD ----------------------------------------------------------------

def _read_jsonld(soup) -> list:
    found: list = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)})[:MAX_JSONLD_BLOCKS]:
        raw = (script.string or script.get_text() or "").strip()
        if not raw or len(raw) > MAX_JSONLD_BYTES:
            continue
        raw = re.sub(r"^\s*<!--|-->\s*$|^\s*//<!\[CDATA\[|//\]\]>\s*$", "", raw).strip()
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        _flatten(parsed, found, 0)
    return found[:100]


def _flatten(node, out: list, depth: int) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node[:200]:
            _flatten(item, out, depth + 1)
    elif isinstance(node, dict):
        if "@graph" in node:
            _flatten(node["@graph"], out, depth + 1)
        else:
            out.append(node)


# ---- dyrektywy robots (meta + X-Robots-Tag) ---------------------------------------

def nofollow_requested(meta_tokens, x_robots_tag: str = "") -> bool:
    """True, gdy strona prosi, by nie podążać za jej linkami (dla nas lub dla wszystkich botów)."""
    tokens = {str(token).lower() for token in (meta_tokens or ())}
    if tokens & {"nofollow", "none"}:
        return True
    for value in (x_robots_tag or "").split("\n"):
        value = value.strip().lower()
        if not value:
            continue
        match = re.match(r"^([\w\-*]+)\s*:\s*(.*)$", value)
        if match and match.group(1) not in _UA_DIRECTIVES:
            if match.group(1) not in _OUR_TOKENS:
                continue  # dyrektywa dla innego bota
            value = match.group(2)
        if {part.strip() for part in value.split(",")} & {"nofollow", "none"}:
            return True
    return False
