"""Domeny poczty publicznej: po samej takiej domenie nie wolno dopasować wiadomości do firmy (czysta logika).

Dwie różne firmy mogą pisać z `gmail.com` albo `wp.pl`, więc wspólna domena niczego nie dowodzi. Dopasowanie po domenie
służy tylko domenom własnym firm (np. `mylimo.pl`).
"""

from __future__ import annotations

PUBLIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.de", "yahoo.fr", "yahoo.es", "ymail.com", "outlook.com",
    "outlook.de", "hotmail.com", "hotmail.co.uk", "hotmail.de", "hotmail.es", "live.com", "live.co.uk", "msn.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "mail.com", "gmx.com", "gmx.de", "gmx.net", "gmx.at", "web.de", "t-online.de", "freenet.de",
    "arcor.de", "posteo.de", "protonmail.com", "proton.me", "pm.me", "tutanota.com", "yandex.com", "yandex.ru", "mail.ru",
    "wp.pl", "o2.pl", "interia.pl", "interia.eu", "poczta.onet.pl", "onet.pl", "onet.eu", "op.pl", "poczta.fm", "gazeta.pl",
    "tlen.pl", "vp.pl", "go2.pl", "autograf.pl", "buziaczek.pl", "amorki.pl", "prokonto.pl", "spoko.pl", "o2.com.pl",
    "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net", "orange.fr", "free.fr", "wanadoo.fr", "laposte.net",
    "libero.it", "alice.it", "tiscali.it", "telefonica.net", "terra.es", "ono.com",
})


def email_domain(address: str) -> str:
    """Domena adresu małymi literami, bez ukośników i białych znaków; pusta, gdy to nie adres."""
    local, _, domain = address.strip().rpartition("@")
    return domain.strip().lower().rstrip(".") if local else ""


def is_public_domain(domain: str) -> bool:
    """Czy domena to poczta publiczna (także jej subdomeny, np. `poczta.wp.pl`)."""
    value = domain.strip().lower().rstrip(".")
    parts = value.split(".")
    return any(".".join(parts[start:]) in PUBLIC_DOMAINS for start in range(len(parts) - 1))


def site_domain(website: str) -> str:
    """Domena strony firmy (`https://www.mylimo.pl/kontakt` → `mylimo.pl`); pusta, gdy adresu nie da się odczytać."""
    text = website.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split("@")[-1].split(":", 1)[0].rstrip(".")
    host = host.removeprefix("www.")
    return host if "." in host and " " not in host else ""
