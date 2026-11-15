"""SSRF guard for owner-supplied `base_url` (OpenAI-compatible endpoints).

The server makes requests to this URL with the owner's key, so it must not be
pointable at internal services (metadata endpoints, Redis, localhost...).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


def normalize_base_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeUrlError("Base URL must use https")
    if not parts.hostname:
        raise UnsafeUrlError("Base URL needs a host")
    if parts.username or parts.password:
        raise UnsafeUrlError("Base URL must not contain credentials")
    if parts.query or parts.fragment:
        raise UnsafeUrlError("Base URL must not contain a query or fragment")
    return url.strip().rstrip("/")


async def assert_public_url(url: str, *, allow_private: bool = False) -> str:
    """Validate scheme/shape and that every resolved address is public.
    Returns the normalized URL. (DNS is re-resolved at request time by httpx; the
    provider layer re-checks before each call to narrow the rebinding window.)"""
    if allow_private:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UnsafeUrlError("Base URL must be http(s) with a host")
        return url.strip().rstrip("/")
    norm = normalize_base_url(url)
    host = urlsplit(norm).hostname or ""
    port = urlsplit(norm).port or 443
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeUrlError("Base URL host does not resolve") from None
    ips = {str(info[4][0]) for info in infos}
    if not ips or not all(_is_public(ip) for ip in ips):
        raise UnsafeUrlError("Base URL must point to a public internet host")
    return norm
