"""Pobieranie jednej strony przez stdlib `http.client`.

Zasady: weryfikacja TLS zawsze włączona (min. TLS 1.2), host rozwiązywany raz i **przypięty IP**
(SNI/Host z nazwy), SSRF-guard (adresy nie-globalne odrzucane), całkowity deadline żądania
egzekwowany zegarem-strażnikiem (obrona przed serwerami sączącymi bajty), limit rozmiaru po
dekompresji (bomba gzip), Content-Type sprawdzany przed czytaniem treści, `Connection: close`.
Redirecty NIE są śledzone tutaj — `fetch_once` zwraca 3xx z `Location`, a crawler decyduje hop po hopie
(każdy przeskok przechodzi przez robots, zakres i bramki uprzejmości).
"""

from __future__ import annotations

import codecs
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from perno.scraper.settings import (
    CONNECT_TIMEOUT,
    MAX_BODY_BYTES,
    RECV_TIMEOUT,
    REQUEST_DEADLINE,
    ROBOTS_MAX_BYTES,
    ScanSettings,
)
from perno.settings import Environment

SMALL_ERROR_BODY = 64 * 1024
SITEMAP_MAX_BYTES = 2 * 1024 * 1024
HTML_TYPES = ("text/html", "application/xhtml+xml")
XML_TYPES = ("application/xml", "text/xml", "text/plain", "application/xhtml+xml")

ACCEPT = {
    "html": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    "robots": "text/plain,*/*;q=0.1",
    "sitemap": "application/xml,text/xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
}
DECODE_CANDIDATES = ("utf_8", "cp1250", "iso8859_2", "cp1252", "latin_1")
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-:.]+)", re.I)


class FetchError(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class FetchResult:
    url: str
    status: Optional[int] = None
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    content_type: str = ""
    elapsed: float = 0.0
    error: Optional[str] = None  # dns | ssrf | timeout | tls | siec | za_duze | niehtml | kodowanie
    location: Optional[str] = None
    ip: str = ""


def is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def make_default_resolver(host_overrides: Optional[Mapping[str, str]] = None) -> Callable:
    """Resolver DNS (IPv4 przed IPv6, najwyżej 3 adresy). `host_overrides` to szew testowy/demo z
    `Environment.scraper_hosts`: niepusty tylko razem z PERNO_SKRAPER_ALLOW_PRIVATE=1, więc w normalnej pracy
    nie da się nim skierować skrapera na adres lokalny."""
    overrides = dict(host_overrides or {})

    def resolve(host: str, port: int) -> list:
        override = overrides.get(host.lower())
        if override:
            return [override]
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ordered = sorted({info[4][0] for info in infos}, key=lambda ip: ":" in ip)
        return ordered[:3]

    return resolve


def _make_ssl_context(cafile: str = "") -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Python 3.13 włącza domyślnie VERIFY_X509_STRICT (odrzuca certyfikaty niezgodne ze ścisłym RFC 5280, np. bez
    # Authority Key Identifier). Przeglądarki i Python <= 3.12 ich nie odrzucają, a skraper ma pobierać strony tak jak
    # one; łańcuch zaufania i nazwa hosta nadal są weryfikowane (wyłączenie weryfikacji jest zabronione, patrz README).
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


@dataclass(frozen=True)
class _Request:
    """Stałe parametry jednego pobrania (wspólne dla prób na kolejnych adresach IP)."""
    scheme: str
    host: str
    port: int
    target: str  # ścieżka z zapytaniem
    kind: str  # html | robots | sitemap
    limit: int  # maks. rozmiar treści (bajty)
    started: float


class Fetcher:
    def __init__(self, settings: ScanSettings, *, resolver: Optional[Callable] = None,  # noqa: PLR0913 — szwy wstrzykiwania z domyślnymi wartościami (testy)
                 allow_private: Optional[bool] = None, ssl_context: Optional[ssl.SSLContext] = None,
                 env: Optional[Environment] = None,
                 request_deadline: float = REQUEST_DEADLINE, connect_timeout: float = CONNECT_TIMEOUT,
                 recv_timeout: float = RECV_TIMEOUT, max_body: int = MAX_BODY_BYTES):
        env = env if env is not None else Environment.from_environ()
        self.settings = settings
        self.resolver = resolver or make_default_resolver(env.scraper_hosts)
        self.allow_private = env.scraper_allow_private if allow_private is None else allow_private
        self.ssl_context = ssl_context or _make_ssl_context(env.ssl_cert_file)
        self.request_deadline = request_deadline
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self.max_body = max_body
        self._dns: dict = {}
        self._dns_lock = threading.Lock()

    # ---- DNS i SSRF --------------------------------------------------------
    def resolve(self, host: str, port: int = 443) -> list:
        with self._dns_lock:
            cached = self._dns.get(host)
        if cached is not None:
            if isinstance(cached, FetchError):
                raise cached
            return cached
        try:
            ips = list(self.resolver(host, port))
        except (socket.gaierror, UnicodeError, OSError) as exc:
            error = FetchError("dns", str(exc))
            with self._dns_lock:
                self._dns[host] = error
            raise error from exc
        if not ips:
            error = FetchError("dns", "brak adresów")
            with self._dns_lock:
                self._dns[host] = error
            raise error
        usable = ips if self.allow_private else [ip for ip in ips if is_public_ip(ip)]
        if not usable:
            error = FetchError("ssrf", f"adres nie-globalny: {', '.join(ips)}")
            with self._dns_lock:
                self._dns[host] = error
            raise error
        with self._dns_lock:
            self._dns[host] = usable
        return usable

    # ---- żądanie -----------------------------------------------------------
    def _headers(self) -> dict:
        headers = {
            "User-Agent": self.settings.user_agent(),
            "Accept-Language": "pl,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        contact = self.settings.contact
        if "@" in contact and "/" not in contact:
            headers["From"] = contact
        return headers

    def fetch_once(self, url: str, kind: str = "html", ip: Optional[str] = None) -> FetchResult:
        parts = urlsplit(url)
        result = FetchResult(url=url)
        started = time.monotonic()
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        request = _Request(
            scheme=parts.scheme, host=host, port=port, kind=kind, started=started,
            target=(parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
            limit={"html": self.max_body, "robots": ROBOTS_MAX_BYTES, "sitemap": SITEMAP_MAX_BYTES}[kind])

        try:
            if ip and not self.allow_private and not is_public_ip(ip):
                raise FetchError("ssrf", f"adres nie-globalny: {ip}")
            candidates = [ip] if ip else self.resolve(host, port)
        except FetchError as exc:
            result.error, result.elapsed = exc.kind, time.monotonic() - started
            return result

        last_error = "siec"
        for address in candidates[:3]:
            verdict, error = self._try_address(address, request, result)
            if verdict == "done":
                return result
            last_error = error
            if verdict == "stop":
                break
        result.error = result.error or last_error
        result.elapsed = time.monotonic() - started
        return result

    def _try_address(self, address: str, request: "_Request", result: FetchResult) -> tuple:
        """Jedna próba pobrania z konkretnego adresu IP. Zwraca (co dalej, ostatni błąd): „done” — wynik gotowy,
        „stop” — nie próbuj innych adresów (inny adres tego samego hosta nie naprawi certyfikatu), „next” — spróbuj
        kolejnego adresu."""
        conn = None
        watchdog = None
        response = None
        state = {"sock": None, "aborted": False}
        try:
            conn = self._connection(request.scheme, request.host, request.port, address)
            deadline = request.started + self.request_deadline
            watchdog = threading.Timer(max(0.05, deadline - time.monotonic()), self._abort, args=(conn, state))
            watchdog.daemon = True
            watchdog.start()
            conn.connect()
            # Przy `Connection: close` http.client zeruje conn.sock po getresponse(), więc trzymamy własne
            # odwołanie — potrzebne i do settimeout w pętli czytania, i do przerwania przez watchdog.
            sock = state["sock"] = conn.sock
            sock.settimeout(self._recv_timeout(deadline))
            headers = self._headers()
            headers["Accept"] = ACCEPT[request.kind]
            conn.request("GET", request.target, headers=headers)
            response = conn.getresponse()
            result.ip = address
            self._read_response(response, result, request.kind, request.limit, deadline, sock)
            result.elapsed = time.monotonic() - request.started
            return "done", ""
        except FetchError as exc:
            result.error = exc.kind
            result.ip = address
            result.elapsed = time.monotonic() - request.started
            return "done", ""
        except ssl.SSLError:
            result.error = "tls"
            result.ip = address
            if state["aborted"]:
                result.error = "timeout"
                return "stop", "timeout"
            return "stop", "tls"
        except (socket.timeout, TimeoutError):
            return "next", "timeout"
        except (ConnectionError, OSError, http.client.HTTPException):
            return "next", "timeout" if state["aborted"] else "siec"
        finally:
            if watchdog is not None:
                watchdog.cancel()
            for closable in (response, conn):
                if closable is not None:
                    try:
                        closable.close()
                    except OSError:
                        pass

    def _connection(self, scheme: str, host: str, port: int, ip: str):
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout, context=self.ssl_context)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
        # Przypięcie IP: nazwa hosta zostaje w Host i SNI, połączenie idzie na rozwiązany adres.
        conn._create_connection = lambda address, timeout, source_address: socket.create_connection(
            (ip, port), timeout, source_address)
        return conn

    @staticmethod
    def _abort(conn, state: dict) -> None:
        """Wywoływane przez zegar-strażnik po przekroczeniu deadline. Tylko shutdown (nie close): zamknięcie
        deskryptora w obcym wątku w trakcie recv grozi użyciem ponownie przydzielonego numeru fd."""
        state["aborted"] = True
        for sock in (state.get("sock"), getattr(conn, "sock", None)):
            if sock is not None:
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)  # omija SSLSocket.shutdown, które zeruje _sslobj
                except OSError:
                    pass

    def _recv_timeout(self, deadline: float) -> float:
        return max(0.05, min(self.recv_timeout, deadline - time.monotonic()))

    def _read_response(self, response, result: FetchResult, kind: str, limit: int, deadline: float, sock) -> None:
        result.status = response.status
        result.headers = {name.lower(): value for name, value in response.getheaders() if name.lower() != "set-cookie"}
        content_type = result.headers.get("content-type", "").split(";")[0].strip().lower()
        result.content_type = content_type
        if 300 <= response.status < 400:
            result.location = result.headers.get("location")
            return
        if response.status >= 400:
            result.body = self._read_body(response, SMALL_ERROR_BODY, deadline, False, result.headers, sock)
            return
        if kind == "html" and content_type and not content_type.startswith(HTML_TYPES):
            result.error = "niehtml"
            return
        if kind == "sitemap" and content_type and content_type not in XML_TYPES:
            result.error = "niehtml"
            return
        declared = result.headers.get("content-length")
        compressed = result.headers.get("content-encoding", "").strip().lower() not in ("", "identity")
        if kind == "html" and declared and declared.isdigit() and int(declared) > (limit * 2 if compressed else limit):
            result.error = "za_duze"
            return
        body = self._read_body(response, limit, deadline, kind == "html", result.headers, sock)
        result.body = body
        if kind == "html" and not content_type and not _looks_like_html(body):
            result.error, result.body = "niehtml", b""

    def _read_body(self, response, limit: int, deadline: float, strict: bool, headers: dict, sock) -> bytes:
        encoding = headers.get("content-encoding", "").strip().lower()
        if encoding not in _SUPPORTED_ENCODINGS:
            if strict:
                raise FetchError("kodowanie", f"nieobsługiwane kodowanie: {encoding}")
            return b""
        decoder = _new_decoder(encoding)
        out = bytearray()
        raw_total = 0
        first = True
        eof = False
        while True:
            if time.monotonic() >= deadline:
                raise FetchError("timeout", "przekroczono całkowity czas żądania")
            if response.isclosed():
                # http.client zamyka gniazdo po ostatnim bajcie odpowiedzi, a od Pythona 3.13 settimeout() na
                # zamkniętym gnieździe rzuca EBADF — zamknięta odpowiedź to to samo co EOF (read1 zwróciłoby b"").
                eof = True
                break
            sock.settimeout(self._recv_timeout(deadline))
            chunk = self._read_chunk(response, deadline)
            if not chunk:
                eof = True
                break
            raw_total += len(chunk)
            if raw_total > 2 * limit + 1024:
                if strict:
                    raise FetchError("za_duze", "za dużo danych skompresowanych")
                break
            decoder = _append_chunk(decoder, out, chunk, limit, retry_raw=first and encoding == "deflate")
            first = False
            if len(out) > limit:
                if strict:
                    raise FetchError("za_duze", "treść przekracza limit")
                return bytes(out[:limit])
        if eof:
            self._require_complete(response, deadline)
        return _finish_body(decoder, out, limit, strict)

    @staticmethod
    def _read_chunk(response, deadline: float) -> bytes:
        try:
            return response.read1(65536)
        except ValueError:  # gniazdo odcięte przez strażnika w trakcie odczytu
            if time.monotonic() >= deadline:
                raise FetchError("timeout", "przekroczono całkowity czas żądania") from None
            raise

    @staticmethod
    def _require_complete(response, deadline: float) -> None:
        """EOF po watchdogu albo przed końcem Content-Length to nie jest kompletna odpowiedź."""
        if time.monotonic() >= deadline:
            raise FetchError("timeout", "przekroczono całkowity czas żądania")
        if response.length:
            raise FetchError("siec", "odpowiedź ucięta przed końcem Content-Length")


_SUPPORTED_ENCODINGS = ("", "identity", "gzip", "x-gzip", "deflate")


def _new_decoder(encoding: str):
    """Dekompresor dla `Content-Encoding` (None, gdy treść nie jest skompresowana)."""
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompressobj(31)
    if encoding == "deflate":
        return zlib.decompressobj(15)
    return None


def _append_chunk(decoder, out: bytearray, chunk: bytes, limit: int, *, retry_raw: bool):
    """Dokleja kawałek odpowiedzi do `out` (po dekompresji, jeśli trzeba); zwraca dekompresor, którego użyć dalej."""
    if decoder is None:
        out += chunk
        return None
    data, decoder = _decompress(decoder, chunk, limit - len(out) + 1, retry_raw=retry_raw)
    out += data
    if decoder.unconsumed_tail:
        del out[limit + 1:]
    return decoder


def _finish_body(decoder, out: bytearray, limit: int, strict: bool) -> bytes:
    """Domyka dekompresję i przycina wynik do limitu (w trybie ścisłym przekroczenie to błąd)."""
    if decoder is not None:
        try:
            out += decoder.flush()
        except zlib.error:
            pass
        if len(out) > limit and strict:
            raise FetchError("za_duze", "treść przekracza limit")
    return bytes(out[:limit]) if len(out) > limit else bytes(out)


def _decompress(decoder, chunk: bytes, max_length: int, *, retry_raw: bool) -> tuple:
    """(dane, dekompresor). Pierwszy kawałek `deflate` bywa surowy (bez nagłówka zlib) — wtedy `retry_raw` pozwala
    spróbować jeszcze raz z dekompresorem surowego deflate."""
    try:
        return decoder.decompress(chunk, max_length), decoder
    except zlib.error:
        if not retry_raw:
            raise FetchError("siec", "uszkodzona treść gzip") from None
        raw = zlib.decompressobj(-15)
        try:
            return raw.decompress(chunk, max_length), raw
        except zlib.error:
            raise FetchError("siec", "uszkodzona treść deflate") from None


def _looks_like_html(body: bytes) -> bool:
    head = body[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<!--", b"<meta", b"<title")) or b"<html" in head


# ---- dekodowanie ----------------------------------------------------------

def _usable(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def decode_html(body: bytes, content_type_header: str = "") -> tuple:
    """(tekst, użyte kodowanie). Kolejność: BOM → nagłówek → <meta> → UTF-8 → charset-normalizer → cp1250."""
    if not body:
        return "", "utf-8"
    for bom, name in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if body.startswith(bom):
            try:
                return body.decode(name), name
            except UnicodeDecodeError:
                break
    candidates = []
    match = _HEADER_CHARSET_RE.search(content_type_header or "")
    candidates.append(_usable(match.group(1)) if match else None)
    meta = _META_CHARSET_RE.search(body[:4096])
    candidates.append(_usable(meta.group(1).decode("ascii", errors="ignore")) if meta else None)
    candidates.append("utf-8")
    for name in candidates:
        if not name:
            continue
        try:
            return body.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        from charset_normalizer import from_bytes  # opcjonalne
        best = from_bytes(body[:32768], cp_isolation=list(DECODE_CANDIDATES)).best()
        if best is not None:
            name = _usable(best.encoding)
            if name:
                return body.decode(name, errors="replace"), name
    except Exception:  # noqa: BLE001, S110 — biblioteka opcjonalna, każdy błąd = przechodzimy dalej
        pass
    return body.decode("cp1250", errors="replace"), "cp1250"
