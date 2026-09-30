"""SSRF guard for the crawler: the app fetches URLs that come from search
results (attacker-influenceable), so every fetch — and every redirect hop —
must resolve to a public web address.

Blocks: non-http(s) schemes, non-web ports, localhost and other blocked
hostnames, and any hostname whose DNS resolution contains a private,
loopback, link-local, carrier-grade-NAT, reserved, multicast or unspecified
address (covers 169.254.169.254 cloud metadata and internal services).

Residual risk (documented in SECURITY_AUDIT.md): a DNS-rebinding attacker
controlling a domain's TTL could still race the check; full mitigation needs
IP pinning at the socket layer, out of scope for this app.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata",
                     "wpad", "instance-data"}
_CGN_NET = ipaddress.ip_network("100.64.0.0/10")

_cache: dict[str, tuple[bool, float]] = {}   # host -> (is_public, expires_at)
_cache_lock = threading.Lock()
_CACHE_TTL = 300
_CACHE_MAX = 20_000


def _ip_is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified):
        return False
    if a.version == 4 and a in _CGN_NET:
        return False
    if a.version == 6 and getattr(a, "ipv4_mapped", None) is not None:
        return _ip_is_public(str(a.ipv4_mapped))
    return True


def _host_is_public(host: str) -> bool:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(host)
        if hit and hit[1] > now:
            return hit[0]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        ips = {info[4][0] for info in infos}
        ok = bool(ips) and all(_ip_is_public(ip) for ip in ips)
    except socket.gaierror:
        ok = False  # unresolvable: the fetch would fail anyway
    with _cache_lock:
        # evict the oldest entries (dicts keep insertion order) instead of
        # clearing everything, which made every crawl thread re-resolve at once
        while len(_cache) >= _CACHE_MAX:
            del _cache[next(iter(_cache))]
        _cache.pop(host, None)
        _cache[host] = (ok, now + _CACHE_TTL)
    return ok


def url_block_reason(url: str) -> str:
    """Empty string when the URL is safe to fetch; otherwise the reason."""
    try:
        p = urlparse(url)
    except ValueError:
        return "unparseable URL"
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        return f"scheme {p.scheme!r} not allowed"
    host = p.hostname
    if not host:
        return "no hostname"
    host = host.strip("[]").lower().rstrip(".")
    try:
        if p.port not in ALLOWED_PORTS:
            return f"port {p.port} not allowed"
    except ValueError:
        return "invalid port"
    if host in BLOCKED_HOSTNAMES or host.endswith((".localhost", ".local",
                                                   ".internal", ".lan")):
        return "internal hostname"
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        if not _ip_is_public(host):
            return "non-public IP address"
        return ""
    if not _host_is_public(host):
        return "resolves to a non-public address (or does not resolve)"
    return ""
