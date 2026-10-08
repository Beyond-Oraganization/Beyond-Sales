"""Treść odpowiedzi: HTML → tekst, czyszczenie, podział na nowy tekst i cytat, skrót i wykrywanie prośby o wypisanie.

Treść pisze osoba trzecia, więc jest niezaufana: usuwamy znaki sterujące, kierunkowe i zerowej szerokości, a panel pokazuje
ją wyłącznie jako tekst. Czysta logika, tylko stdlib (`HTMLParser`), bez bs4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩؜﻿]")
_BLOCKS = frozenset({"p", "div", "li", "tr", "table", "ul", "ol", "pre", "hr", "section", "article", "header", "footer",
                     "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "address", "form"})
_SKIPPED = frozenset({"script", "style", "head", "title", "noscript", "template", "svg", "object", "iframe"})
_SNIPPET = 160


def strip_unsafe(text: str) -> str:
    """Tekst bez znaków sterujących, kierunkowych i zerowej szerokości; twarda spacja staje się zwykłą, \\r\\n → \\n."""
    return _UNSAFE.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " "))


class _Extractor(HTMLParser):
    """Zamienia HTML na tekst; `<blockquote>` daje linie z prefiksem „> ”, żeby cytat dał się rozpoznać jak w poczcie tekstowej."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list = []
        self._buffer: list = []
        self._skip = 0
        self._quote = 0
        self._pre = 0

    def _flush(self, blank: bool = False) -> None:
        """Kończy bieżącą linię; pusta zostaje pominięta, chyba że `blank` (drugi `<br>` z rzędu to pusta linia)."""
        text = "".join(self._buffer).strip() if not self._pre else "".join(self._buffer).rstrip()
        self._buffer = []
        if text or self._pre:
            self.lines.append(("> " * self._quote) + text)
        elif blank and self.lines and self.lines[-1] != "":
            self.lines.append("")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "br":
            self._flush(blank=True)
        elif tag == "blockquote":
            self._flush()
            self._quote += 1
        elif tag in _BLOCKS:
            self._flush()
            if tag == "li":
                self._buffer.append("- ")
            if tag == "pre":
                self._pre += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag == "blockquote":
            self._flush()
            self._quote = max(0, self._quote - 1)
        elif tag in _BLOCKS:
            self._flush()
            if tag == "pre":
                self._pre = max(0, self._pre - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._buffer.append(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> str:
    """Tekst z HTML (bez skryptów i stylów), z podziałem na linie i cytatami jako „> ”."""
    parser = _Extractor()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError):  # zepsuty HTML nie może zablokować pobierania poczty
        pass
    parser._flush()
    return normalize("\n".join(parser.lines))


def normalize(text: str) -> str:
    """Tekst po czyszczeniu: bez znaków niebezpiecznych, spacji na końcach linii i więcej niż jednej pustej linii z rzędu."""
    cleaned = strip_unsafe(text)
    lines = [line.rstrip() for line in cleaned.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def body_text(plain: str, html: str) -> str:
    """Treść do zapisu: wersja tekstowa, a gdy jej nie ma albo jest pusta, tekst wyciągnięty z HTML."""
    text = normalize(plain or "")
    return text if text else html_to_text(html or "")


# ---- cytat -------------------------------------------------------------------------------------

_VERB = r"(wrote|napisał|napisała|napisał\(a\)|napisało|schrieb|a écrit|escribió|ha scritto|skrev|schreef)"
_INTRO = re.compile(r"^\s*(on|w dniu|dnia|am|le|el|il)\b.{3,300}$", re.IGNORECASE)
_HAS_VERB = re.compile(rf"\b{_VERB}\b", re.IGNORECASE)
_ENDS_WITH_VERB = re.compile(rf"\b{_VERB}\s*:?\s*$", re.IGNORECASE)
_WROTE_TAIL = re.compile(rf"^.{{0,200}}\b{_VERB}\s*:?\s*$", re.IGNORECASE)
_MARKER = re.compile(r"^\s*(-{2,}\s*(original message|oryginalna wiadomość|ursprüngliche nachricht|mensaje original|"
                     r"wiadomość oryginalna|forwarded message|przekazana wiadomość)\s*-{2,}|_{10,})\s*$", re.IGNORECASE)
_HEADER_FROM = re.compile(r"^\s*(from|od|von|de|da)\s*:\s*\S", re.IGNORECASE)
_HEADER_SENT = re.compile(r"^\s*(sent|wysłano|gesendet|enviado|date|data|datum|inviato)\s*:\s*\S", re.IGNORECASE)


@dataclass(frozen=True)
class QuoteSplit:
    """Treść podzielona na to, co firma napisała nowego, i cytat naszej wiadomości (nic nie jest wyrzucane)."""

    new: str
    quoted: str

    @property
    def has_quote(self) -> bool:
        return bool(self.quoted)


def _attribution_length(lines: list, index: int) -> int:
    """Ile linii zajmuje wiersz „Dnia … napisał(a):” zaczynający się w `index` (0, gdy to nie on); bywa zawinięty na dwie."""
    line = lines[index].rstrip()
    if _INTRO.match(line) and _HAS_VERB.search(line) and (line.endswith(":") or _ENDS_WITH_VERB.search(line)):
        return 1
    if index + 1 < len(lines) and _INTRO.match(line) and _WROTE_TAIL.match(lines[index + 1].rstrip()):
        return 2
    return 0


def _starts_header_block(lines: list, index: int) -> bool:
    if _MARKER.match(lines[index]):
        return True
    return bool(_HEADER_FROM.match(lines[index])
                and any(_HEADER_SENT.match(line) for line in lines[index + 1:index + 4]))


def _next_content(lines: list, index: int) -> str:
    return next((line for line in lines[index:] if line.strip()), "")


def split_quote(text: str) -> QuoteSplit:
    """Dzieli treść na nowy tekst i cytat. Odpowiedź pod cytatem (linie „> ” nad nią) zostaje w nowym tekście. Wszystko od
    nagłówka „Od: … Wysłano: …”, od „-----Original Message-----” albo od wiersza „Dnia … napisał(a):” bez dalszych „> ” to cytat
    do końca (taki klient nie oznacza cytatu inaczej, a odpowiedź pisze się nad nim). Gdyby po podziale nic nowego nie
    zostało, całość jest nowym tekstem: lepiej pokazać za dużo niż zgubić odpowiedź."""
    lines = text.split("\n")
    fresh: list = []
    quoted: list = []
    held: list = []  # puste linie czekają, aż wiadomo, do której części należą (do tej samej, co następna niepusta linia)
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            held.append(line)
            index += 1
            continue
        if _starts_header_block(lines, index):
            quoted.extend(held + lines[index:])
            held = []
            break
        span = 0 if line.lstrip().startswith(">") else _attribution_length(lines, index)
        if span and not _next_content(lines, index + span).lstrip().startswith(">"):
            quoted.extend(held + lines[index:])  # po wierszu autora nie ma „> ”: reszta to nieoznaczony cytat
            held = []
            break
        if span or line.lstrip().startswith(">"):
            quoted.extend(held + lines[index:index + max(span, 1)])
            index += max(span, 1)
        else:
            fresh.extend(held + [line])
            index += 1
        held = []
    new_text = normalize("\n".join(fresh))
    if not new_text and quoted:
        return QuoteSplit(normalize("\n".join(quoted)), "")
    return QuoteSplit(new_text, normalize("\n".join(quoted)))


def snippet(text: str, limit: int = _SNIPPET) -> str:
    """Jedna linia z początku tekstu do listy wątków (z „…”, gdy ucięto)."""
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def collapse(text: str) -> str:
    """Tekst w jednej linii, bez znaków niebezpiecznych (do skrótów dla AI i logów)."""
    return re.sub(r"\s+", " ", strip_unsafe(text)).strip()


# ---- prośba o wypisanie --------------------------------------------------------------------

_OPT_OUT = re.compile("|".join((
    r"\bwypis", r"\bwypisa[ćc]", r"usu[ńn]\w*\s+(mnie|nas|mój|nasz|moje|nasze)", r"nie\s+(życzę|życzymy|chcę|chcemy)\s+sobie",
    r"proszę\s+(o\s+)?(nie\s+(pisać|wysyłać|kontaktować)|zaprzestanie|zaprzestać)", r"zaprzesta", r"nie\s+kontaktuj",
    r"nie\s+wysyłaj", r"cofam\s+zgod", r"\bRODO\b", r"skreśl\w*\s+(mnie|nas)",
    r"unsubscribe", r"remove\s+(me|us)\b", r"take\s+(me|us)\s+off", r"stop\s+(emailing|sending|contacting|writing)",
    r"do\s+not\s+(contact|email|write)", r"don'?t\s+(contact|email|write)", r"opt[- ]?out", r"\bgdpr\b",
    r"abmelden", r"austragen", r"nicht\s+mehr\s+(kontaktieren|anschreiben|schreiben)", r"keine\s+(weiteren\s+)?(e-?mails|werbung|nachrichten)",
    r"darse\s+de\s+baja", r"no\s+(me\s+)?(contacte|escriba|envíe)", r"eliminar\s+mis\s+datos",
)), re.IGNORECASE)


def optout_hint(new_text: str) -> bool:
    """Czy nowy tekst odpowiedzi wygląda na prośbę o zaprzestanie kontaktu (wskazówka dla człowieka, nie decyzja).
    Sprawdzamy tylko nowy tekst: cytat naszej wiadomości też mógłby zawierać takie słowa."""
    return bool(_OPT_OUT.search(new_text))
