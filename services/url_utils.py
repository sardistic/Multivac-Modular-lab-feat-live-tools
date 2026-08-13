"""Bounded public-URL retrieval shared by page and media features."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import os
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Avoid compressed-body bombs. A server that ignores this is rejected.
    "Accept-Encoding": "identity",
}
DEFAULT_PAGE_BYTES = max(64_000, int(os.getenv("URL_FETCH_MAX_PAGE_BYTES", "2000000")))
DEFAULT_MEDIA_BYTES = max(256_000, int(os.getenv("URL_FETCH_MAX_MEDIA_BYTES", "12000000")))
MAX_REDIRECTS = max(0, min(int(os.getenv("URL_FETCH_MAX_REDIRECTS", "4")), 8))
MAX_URL_LENGTH = 4096


class UnsafeURLError(ValueError):
    """The URL is not a permitted public HTTP(S) destination."""


class URLFetchError(RuntimeError):
    """A permitted URL could not be fetched within the bounded policy."""


@dataclass(frozen=True)
class FetchedURL:
    body: bytes
    content_type: str
    final_url: str
    status: int


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection(
            (self._pinned_address, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _allowed_ports() -> set[int]:
    raw = os.getenv("URL_FETCH_ALLOWED_PORTS", "80,443")
    ports: set[int] = set()
    for value in raw.split(","):
        try:
            port = int(value.strip())
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports or {80, 443}


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise URLFetchError("destination hostname could not be resolved") from exc
        addresses = []
        seen = set()
        for item in resolved:
            raw = item[4][0]
            if raw in seen:
                continue
            seen.add(raw)
            try:
                addresses.append(ipaddress.ip_address(raw))
            except ValueError:
                continue

    if not addresses:
        raise URLFetchError("destination hostname returned no usable addresses")
    if any(not address.is_global for address in addresses):
        raise UnsafeURLError("private, loopback, link-local, or reserved destinations are blocked")
    return [str(address) for address in addresses]


def validate_public_http_url(url: str) -> tuple[str, str, int, str]:
    """Validate a URL and return scheme, host, port, and one pinned public IP."""
    value = (url or "").strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise UnsafeURLError("URL is empty or too long")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL contains an invalid port") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("only HTTP and HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URLs containing credentials are blocked")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise UnsafeURLError("URL has no hostname")
    port = port or (443 if scheme == "https" else 80)
    if port not in _allowed_ports():
        raise UnsafeURLError("destination port is not allowed")
    addresses = _public_addresses(hostname, port)
    return scheme, hostname, port, addresses[0]


def _host_header(hostname: str, port: int, scheme: str) -> str:
    rendered = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return rendered if port == default_port else f"{rendered}:{port}"


def _read_bounded(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    raw_length = response.getheader("Content-Length")
    if raw_length:
        try:
            if int(raw_length) > max_bytes:
                raise URLFetchError(f"response exceeds the {max_bytes:,}-byte limit")
        except ValueError:
            pass
    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise URLFetchError("compressed responses are not accepted")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise URLFetchError(f"response exceeds the {max_bytes:,}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_url_bytes(
    url: str,
    *,
    timeout: float = 15,
    max_bytes: int = DEFAULT_MEDIA_BYTES,
    allowed_content_types: tuple[str, ...] | None = None,
    headers: dict[str, str] | None = None,
) -> FetchedURL:
    """Fetch a public URL while pinning validated DNS and bounding every hop."""
    current = (url or "").strip()
    max_bytes = max(1, int(max_bytes))

    for redirect_count in range(MAX_REDIRECTS + 1):
        scheme, hostname, port, address = validate_public_http_url(current)
        parsed = urlsplit(current)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_headers = {
            **DEFAULT_HEADERS,
            "Accept": "*/*",
            "Host": _host_header(hostname, port, scheme),
            **(headers or {}),
        }
        connection_cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
        connection = connection_cls(hostname, address, port, float(timeout))
        try:
            connection.request("GET", target, headers=request_headers)
            response = connection.getresponse()
            status = int(response.status)
            if status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(0)
                if not location:
                    raise URLFetchError("redirect response omitted its destination")
                if redirect_count >= MAX_REDIRECTS:
                    raise URLFetchError("too many redirects")
                current = urljoin(current, location)
                continue
            if status < 200 or status >= 300:
                raise URLFetchError(f"remote server returned HTTP {status}")
            content_type = (response.getheader("Content-Type") or "").lower()
            if allowed_content_types and not any(
                content_type.startswith(prefix.lower()) for prefix in allowed_content_types
            ):
                raise URLFetchError("remote response has an unsupported content type")
            body = _read_bounded(response, max_bytes)
            return FetchedURL(
                body=body,
                content_type=content_type,
                final_url=current,
                status=status,
            )
        except (UnsafeURLError, URLFetchError):
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise URLFetchError("remote connection failed") from exc
        finally:
            connection.close()

    raise URLFetchError("too many redirects")


async def fetch_url_bytes_async(*args, **kwargs) -> FetchedURL:
    return await asyncio.to_thread(fetch_url_bytes, *args, **kwargs)


def fetch_url_content(url: str, timeout: int = 15) -> str:
    fetched = fetch_url_bytes(
        url,
        timeout=timeout,
        max_bytes=DEFAULT_PAGE_BYTES,
        allowed_content_types=("text/html", "text/plain", "application/xhtml+xml"),
        headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
    )
    charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", fetched.content_type)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return fetched.body.decode(charset, errors="replace")
    except LookupError:
        return fetched.body.decode("utf-8", errors="replace")


def extract_main_text(html: str) -> tuple[str, str]:
    """Very lightweight content extraction returning (title, text)."""
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    for sel in ["article", "[role=main]", "main", ".post", ".article", ".entry-content"]:
        candidates.extend(soup.select(sel))

    def node_text(node):
        for bad in node(["script", "style", "noscript", "nav", "aside", "form", "footer", "header"]):
            bad.decompose()
        txt = " ".join(t.get_text(" ", strip=True) for t in node.find_all(["p", "li", "h2", "h3", "h4"]))
        return re.sub(r"\s+", " ", txt).strip()

    text = ""
    if candidates:
        biggest = max(candidates, key=lambda n: len(n.get_text(strip=True)))
        text = node_text(biggest)

    if len(text) < 400:
        for bad in soup(["script", "style", "noscript", "nav", "aside", "form", "footer", "header"]):
            bad.decompose()
        text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))
        text = re.sub(r"\s+", " ", text).strip()

    title = (soup.title.get_text(strip=True) if soup.title else "")[:300]
    return title, text


def reduce_text_length(text: str, max_chars: int = 3000) -> str:
    if len(text) <= max_chars:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    total = 0
    for part in parts:
        if total + len(part) + 1 > max_chars:
            break
        out.append(part)
        total += len(part) + 1
    value = " ".join(out).strip()
    if not value:
        value = text[: max_chars - 1]
    return value + "…"
