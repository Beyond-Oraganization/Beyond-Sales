"""Podstawianie {{token}} w szablonach HTML. Bez zewnętrznych zależności."""

import html
import re

TOKEN_RE = re.compile(r"\{\{(\w+)\}\}")


class MissingTokenError(ValueError):
    """W szablonie zostały nierozpoznane tokeny `{{…}}`."""
    pass


def render(template: str, values: dict, escape: bool = True) -> str:
    """Podstawia {{klucz}} wartościami ze `values`. Podnosi błąd, jeśli po
    podstawieniu zostały jeszcze jakieś nierozpoznane tokeny — lepiej się
    wywalić niż po cichu wysłać surowy `{{token}}` do klienta."""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = str(values[key])
        return html.escape(value) if escape else value

    out = TOKEN_RE.sub(_sub, template)
    leftover = TOKEN_RE.findall(out)
    if leftover:
        raise MissingTokenError(f"Nierozpoznane tokeny w szablonie: {leftover}")
    return out


def drop_empty_paragraphs(html_body: str) -> str:
    """Usuwa puste akapity `<p></p>` (np. opcjonalne zdanie, którego firma nie ma)."""
    return re.sub(r"<p>\s*</p>\s*", "", html_body)


def _link_to_text(match: re.Match) -> str:
    href, label = match.group(1), match.group(2).strip()
    return label if label == href else f"{label} ({href})"


def html_to_text(html_body: str) -> str:
    """Bardzo prosty konwerter HTML -> plain text dla wersji text/plain maila."""
    text = re.sub(r"(?is)<head\b.*?</head>", "", html_body)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "* ", text)
    text = re.sub(r"(?is)<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", _link_to_text, text)
    text = re.sub(r"(?is)<img[^>]*alt=[\"']([^\"']*)[\"'][^>]*>", r"[\1]", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
