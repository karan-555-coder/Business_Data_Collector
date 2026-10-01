"""One verified TLS context for every HTTPS connection in the process.

requests (2.34) hands urllib3 the CA bundle PATH for every connection pool,
and urllib3 then builds a fresh SSLContext and parses that bundle on EVERY
new TLS connection. Measured: ~320 ms CPU per connection (OpenSSL 3 parsing
certifi's 240 KB PEM on Windows) - ten times the cost of parsing the page
itself. A crawler opens a new connection to almost every site it visits,
so on a 0.1-CPU host this dominated the crawl.

SharedTLSAdapter loads the bundle once into a single context and gives it
to every pool. Verification is unchanged: the context requires a valid
chain (CERT_REQUIRED) and a matching hostname, exactly as before. Only
`verify=True` (the default) uses it; a custom CA bundle path (verify="...",
REQUESTS_CA_BUNDLE) or verify=False keeps requests' own per-pool handling.
"""

from __future__ import annotations

import ssl
import threading

from requests.adapters import HTTPAdapter
from requests.utils import DEFAULT_CA_BUNDLE_PATH
from urllib3.util.ssl_ import create_urllib3_context

_ctx: ssl.SSLContext | None = None
_ctx_lock = threading.Lock()


def shared_tls_context() -> ssl.SSLContext:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            ctx = create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
            ctx.load_verify_locations(DEFAULT_CA_BUNDLE_PATH)
            _ctx = ctx
        return _ctx


class SharedTLSAdapter(HTTPAdapter):
    """HTTPAdapter whose verified HTTPS pools share one pre-loaded context."""

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert)
        if verify is True and host_params.get("scheme") == "https":
            pool_kwargs["ssl_context"] = shared_tls_context()
        return host_params, pool_kwargs

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if verify is True and url.lower().startswith("https"):
            # the CAs are already in the shared context; a path here would
            # make urllib3 re-parse the bundle into it on every connection
            conn.ca_certs = None
            conn.ca_cert_dir = None
