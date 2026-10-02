"""Lokalny „internet" do testów skrapera: jeden ThreadingHTTPServer na 127.0.0.1:0 udaje wiele
witryn (rozróżnianych nagłówkiem Host), loguje żądania i mierzy równoległość po stronie serwera.
Żaden test skrapera nie może wyjść do prawdziwej sieci — `NoNetworkMixin` to egzekwuje."""

from __future__ import annotations

import gzip
import socket
import socketserver
import ssl
import threading
import time
import unittest
import zlib
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from unittest import mock

LOCAL_HOSTS = ("127.0.0.1", "::1", "localhost")


class Page:
    def __init__(self, body="", status=200, content_type: Optional[str] = "text/html; charset=utf-8",
                 headers: Optional[dict] = None, delay: float = 0.0, header_delay: float = 0.0,
                 encoding: Optional[str] = None, chunked: bool = False, location: Optional[str] = None,
                 trickle: Optional[tuple] = None, content_length: Optional[int] = None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.content_type = content_type
        self.headers = headers or {}
        self.delay = delay
        self.header_delay = header_delay
        self.encoding = encoding  # gzip | deflate | raw-deflate | br
        self.chunked = chunked
        self.location = location
        self.trickle = trickle  # (bajtów na krok, sekund na krok)
        self.content_length = content_length

    def payload(self) -> tuple:
        body, header = self.body, None
        if self.encoding == "gzip":
            body, header = gzip.compress(body), "gzip"
        elif self.encoding == "deflate":
            body, header = zlib.compress(body), "deflate"
        elif self.encoding == "raw-deflate":
            compressor = zlib.compressobj(wbits=-15)
            body, header = compressor.compress(body) + compressor.flush(), "deflate"
        elif self.encoding == "br":
            header = "br"
        return body, header


def html(*links, title="Strona", extra=""):
    anchors = "".join(f'<a href="{href}">{href}</a> ' for href in links)
    return f"<html><head><title>{title}</title></head><body><p>{title} " + ("treść " * 60) + f"</p>{anchors}{extra}</body></html>"


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def server_bind(self):  # bez odwrotnego DNS (getfqdn) przy starcie
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class FakeInternet:
    def __init__(self, tls: Optional[tuple] = None, port: int = 0):
        self.requested_port = port
        self.sites: dict = {}
        self.log: list = []
        self.inflight: dict = defaultdict(int)
        self.max_inflight: dict = defaultdict(int)
        self.total_inflight = 0
        self.max_total = 0
        self.lock = threading.Lock()
        self.tls = tls
        self.port = 0
        self._server = None
        self._thread = None

    # ---- konfiguracja --------------------------------------------------------
    def site(self, host: str, pages: Optional[dict] = None, robots=None) -> dict:
        entry = {"pages": dict(pages or {}), "robots": robots}
        self.sites[host.lower()] = entry
        return entry

    def url(self, host: str, path: str = "/") -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{host}:{self.port}{path}"

    def hits(self, host: Optional[str] = None, path: Optional[str] = None) -> list:
        return [e for e in self.log if (host is None or e["host"] == host) and (path is None or e["path"] == path)]

    def max_overlap(self, hosts=None, min_busy: float = 0.05) -> int:
        """Największa liczba żądań jednocześnie „zajętych" po stronie serwera. Liczymy tylko okno sztucznego opóźnienia
        strony (start → koniec opóźnienia), więc mikro-wyścig przy zamykaniu połączenia nie fałszuje wyniku."""
        spans = [(e["t"], e["busy_end"]) for e in self.log
                 if e.get("busy_end") and e["busy_end"] - e["t"] > min_busy and (hosts is None or e["host"] in hosts)]
        events = sorted([(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans])
        current = best = 0
        for _, delta in events:
            current += delta
            best = max(best, current)
        return best

    # ---- cykl życia -----------------------------------------------------------
    def start(self) -> "FakeInternet":
        internet = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                internet._serve(self)

        server = _QuietServer(("127.0.0.1", self.requested_port), Handler)
        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*self.tls)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(5)

    # ---- obsługa żądania --------------------------------------------------------
    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        host_header = handler.headers.get("Host", "")
        host = host_header.split(":")[0].lower()
        entry = {
            "t": time.monotonic(), "host": host, "path": handler.path, "host_header": host_header,
            "ua": handler.headers.get("User-Agent"), "from": handler.headers.get("From"),
            "accept": handler.headers.get("Accept"), "accept_encoding": handler.headers.get("Accept-Encoding"),
            "connection": handler.headers.get("Connection"), "cookie": handler.headers.get("Cookie"),
            "status": None,
        }
        with self.lock:
            self.log.append(entry)
            self.inflight[host] += 1
            self.max_inflight[host] = max(self.max_inflight[host], self.inflight[host])
            self.total_inflight += 1
            self.max_total = max(self.max_total, self.total_inflight)
        try:
            page = self._page_for(host, handler.path, entry)
            entry["status"] = page.status
            self._send(handler, page, entry)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass
        finally:
            with self.lock:
                self.inflight[host] -= 1
                self.total_inflight -= 1

    def _page_for(self, host: str, path: str, entry: dict) -> Page:
        site = self.sites.get(host)
        if site is None:
            return Page("<html>nieznany host</html>", status=404)
        if path == "/robots.txt":
            robots = site["robots"]
            if robots is None:
                return Page("not found", status=404, content_type="text/plain")
            return robots(entry) if callable(robots) else (robots if isinstance(robots, Page) else Page(robots, content_type="text/plain"))
        page = site["pages"].get(path)
        if callable(page):
            page = page(entry)
        return page if page is not None else Page("<html><body>404</body></html>", status=404)

    def _send(self, handler: BaseHTTPRequestHandler, page: Page, entry: dict) -> None:
        if page.header_delay:
            time.sleep(page.header_delay)
        if page.delay:
            time.sleep(page.delay)
        entry["busy_end"] = time.monotonic()
        body, content_encoding = page.payload()
        handler.send_response(page.status)
        if page.content_type:
            handler.send_header("Content-Type", page.content_type)
        if content_encoding:
            handler.send_header("Content-Encoding", content_encoding)
        if page.location:
            handler.send_header("Location", page.location)
        for name, value in page.headers.items():
            handler.send_header(name, value)
        handler.send_header("Connection", "close")
        if page.chunked:
            handler.send_header("Transfer-Encoding", "chunked")
        else:
            handler.send_header("Content-Length", str(page.content_length if page.content_length is not None else len(body)))
        handler.end_headers()
        handler.close_connection = True
        if page.chunked:
            for start in range(0, len(body), 4096):
                chunk = body[start:start + 4096]
                handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")
        elif page.trickle:
            step, pause = page.trickle
            for start in range(0, len(body), step):
                handler.wfile.write(body[start:start + step])
                handler.wfile.flush()
                time.sleep(pause)
        else:
            handler.wfile.write(body)
        handler.wfile.flush()


class NoNetworkMixin(unittest.TestCase):
    """Żaden test skrapera nie może łączyć się z czymkolwiek poza pętlą zwrotną."""

    def setUp(self):
        super().setUp()
        real_create = socket.create_connection

        def guarded_create(address, *args, **kwargs):
            if address[0] not in LOCAL_HOSTS:
                raise AssertionError(f"test próbował połączyć się z {address[0]!r}")
            return real_create(address, *args, **kwargs)

        real_getaddrinfo = socket.getaddrinfo

        def guarded_getaddrinfo(host, *args, **kwargs):
            if host not in LOCAL_HOSTS:
                raise AssertionError(f"test próbował rozwiązać DNS dla {host!r}")
            return real_getaddrinfo(host, *args, **kwargs)

        for target, replacement in (("socket.create_connection", guarded_create),
                                    ("socket.getaddrinfo", guarded_getaddrinfo)):
            patcher = mock.patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)


def local_resolver(host: str, port: int) -> list:
    return ["127.0.0.1"]
