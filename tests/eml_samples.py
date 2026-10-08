"""Przykładowe wiadomości przychodzące w postaci surowych bajtów (RFC 5322) do testów odbioru odpowiedzi.

Wiadomości są wymyślone, ale odwzorowują rzeczywiste kształty, z którymi trzeba sobie radzić: odpowiedź nad cytatem i pod
cytatem, sam HTML, autoodpowiedź, raport doręczenia (DSN) z oryginałem, zwrot bez raportu, nazwy w kodowaniu RFC 2047.
"""

from __future__ import annotations

CRLF = "\r\n"


def raw(text: str) -> bytes:
    """Zamienia tekst z końcami linii \\n na bajty z CRLF (tak jak wysyła je serwer)."""
    return text.strip("\n").replace("\n", CRLF).encode("utf-8") + CRLF.encode()


def plain_reply(*, sender: str = "anna@mylimo.pl", to: str = "roman@z3x.pl", subject: str = "Re: Rezerwacje limuzyn, Kraków",
                body: str = "Dzień dobry, proszę o cennik.", message_id: str = "<odp1@mylimo.pl>",
                in_reply_to: str = "", references: str = "", date: str = "Fri, 02 Oct 2026 12:00:00 +0200", extra: str = "") -> bytes:
    headers = [f"From: Anna Nowak <{sender}>", f"To: {to}", f"Subject: {subject}", f"Date: {date}", f"Message-ID: {message_id}"]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if references:
        headers.append(f"References: {references}")
    if extra:
        headers.append(extra)
    headers += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "Content-Transfer-Encoding: 8bit"]
    return raw("\n".join(headers) + "\n\n" + body)


def html_only_reply() -> bytes:
    return raw("""From: Jan <jan@firma.pl>
To: roman@z3x.pl
Subject: Re: Konsultacje dla spółdzielni, Katowice
Date: Fri, 02 Oct 2026 13:00:00 +0200
Message-ID: <html1@firma.pl>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><style>p{}</style><div>Zainteresowani. Proszę o kontakt.</div>
<div>W dniu pt., 2 paź 2026 o 10:00 Roman &lt;roman@z3x.pl&gt; napisał(a):</div>
<blockquote>Dzień dobry, piszę w sprawie płatności.</blockquote></body></html>""")


def multipart_reply_with_attachment() -> bytes:
    return raw("""From: =?UTF-8?Q?Zbigniew_Wi=C5=9Bniewski?= <zbyszek@sklep.pl>
To: roman@z3x.pl
Subject: =?UTF-8?B?UmU6IFRlbWF0IHogxIUgxJk=?=
Date: Fri, 02 Oct 2026 14:00:00 +0200
Message-ID: <mp1@sklep.pl>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BND"

--BND
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset=utf-8

Wersja tekstowa odpowiedzi.
--ALT
Content-Type: text/html; charset=utf-8

<p>Wersja <b>HTML</b> odpowiedzi.</p>
--ALT--
--BND
Content-Type: application/pdf; name="oferta.pdf"
Content-Disposition: attachment; filename="oferta.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--BND--""")


def auto_reply() -> bytes:
    return raw("""From: biuro@firma.pl
To: roman@z3x.pl
Subject: Automatic reply: Rezerwacje limuzyn, Kraków
Date: Fri, 02 Oct 2026 12:05:00 +0200
Message-ID: <auto1@firma.pl>
Auto-Submitted: auto-replied
X-Auto-Response-Suppress: All
Content-Type: text/plain; charset=utf-8

Jestem poza biurem do 10.10.""")


def dsn_bounce(*, recipient: str = "nieistnieje@firma.pl", original_id: str = "<orig1@z3x.pl>", status: str = "5.1.1",
               action: str = "failed", as_message: bool = False) -> bytes:
    original = (f"Message-ID: {original_id}\nFrom: roman@z3x.pl\nTo: {recipient}\nSubject: Rezerwacje limuzyn, Kraków\n")
    tail = ("Content-Type: message/rfc822\n\n" + original) if as_message else ("Content-Type: text/rfc822-headers\n\n" + original)
    return raw(f"""From: Mail Delivery System <MAILER-DAEMON@mx.z3x.pl>
To: roman@z3x.pl
Subject: Undelivered Mail Returned to Sender
Date: Fri, 02 Oct 2026 12:01:00 +0200
Message-ID: <bounce1@mx.z3x.pl>
Auto-Submitted: auto-replied
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="DSN"

--DSN
Content-Type: text/plain; charset=utf-8

To jest automatyczna wiadomość. Nie udało się dostarczyć wiadomości do {recipient}.
--DSN
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.z3x.pl

Final-Recipient: rfc822; {recipient}
Original-Recipient: rfc822;{recipient}
Action: {action}
Status: {status}
Diagnostic-Code: smtp; 550 5.1.1 <{recipient}>: Recipient address rejected: User unknown
--DSN
{tail}
--DSN--""")


def text_bounce(*, recipient: str = "ktos@firma.pl", phrase: str = "550 5.1.1 User unknown") -> bytes:
    return raw(f"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: roman@z3x.pl
Subject: Delivery Status Notification (Failure)
Date: Fri, 02 Oct 2026 12:02:00 +0200
Message-ID: <bounce2@googlemail.com>
X-Failed-Recipients: {recipient}
Content-Type: text/plain; charset=utf-8

Wiadomość nie została dostarczona do {recipient}.
Odpowiedź serwera: {phrase}""")


def soft_bounce(recipient: str = "pelna@firma.pl") -> bytes:
    return raw(f"""From: postmaster@mx.firma.pl
To: roman@z3x.pl
Subject: Mail delivery failed: returning message to sender
Date: Fri, 02 Oct 2026 12:03:00 +0200
Message-ID: <bounce3@mx.firma.pl>
Auto-Submitted: auto-generated
Content-Type: text/plain; charset=utf-8

Skrzynka {recipient} jest chwilowo przepełniona. Status 4.2.2, spróbuj później.""")


def newsletter() -> bytes:
    return raw("""From: Sklep <newsletter@sklep.example>
To: roman@z3x.pl
Subject: Wielka wyprzedaż!
Date: Fri, 02 Oct 2026 09:00:00 +0200
Message-ID: <news1@sklep.example>
Precedence: bulk
List-Unsubscribe: <mailto:unsub@sklep.example>
Content-Type: text/plain; charset=utf-8

Kup teraz.""")


def internal_forward() -> bytes:
    return raw("""From: Jakub <jakub@z3x.io>
To: roman@z3x.pl
Subject: Fwd: Re: Rezerwacje limuzyn, Kraków
Date: Fri, 02 Oct 2026 15:00:00 +0200
Message-ID: <fwd1@z3x.io>
Content-Type: text/plain; charset=utf-8

Odpisz mu, że może zadzwonić.""")
