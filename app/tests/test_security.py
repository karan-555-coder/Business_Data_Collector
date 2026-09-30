"""Security unit tests: SSRF guard, Excel formula-injection guard, download
whitelist. Run:  python tests/test_security.py  (from the app/ directory)"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.collector.ssrf import url_block_reason  # noqa: E402
from backend.collector.normalize import xlsx_safe  # noqa: E402


def main():
    blocked = [
        "http://localhost/admin",
        "http://127.0.0.1:8100/api/config",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/router",
        "http://172.16.0.10/",
        "http://100.64.0.1/",                          # carrier-grade NAT
        "file:///C:/Windows/win.ini",
        "ftp://example.com/data",
        "gopher://example.com/",
        "http://example.com:22/",                      # non-web port
        "http://printer.local/",
        "http://intranet.internal/",
        "http://0.0.0.0/",
    ]
    for url in blocked:
        reason = url_block_reason(url)
        assert reason, f"SSRF guard failed to block: {url}"
        print(f"  blocked  {url}  ({reason})")

    allowed = ["https://example.com/", "http://example.com:8080/page",
               "https://serper.dev/"]
    for url in allowed:
        reason = url_block_reason(url)
        assert not reason, f"SSRF guard wrongly blocked {url}: {reason}"
        print(f"  allowed  {url}")

    # Excel formula injection: leading = + @ must be neutralized
    for evil in ("=HYPERLINK(\"http://evil\")", "+SUM(1,1)", "@cmd"):
        safe = xlsx_safe(evil)
        assert not safe.startswith(("=", "+", "@")), f"formula not neutralized: {safe!r}"
    print("  xlsx formula-injection guard OK")

    # Download endpoint accepts only whitelisted generated filenames
    from backend.collector.engine import StateStore
    from backend.collector.categories import CATEGORIES, SUMMARY_FILE
    st = StateStore()
    known = {d["file"] for d in st.all_categories().values()} | {SUMMARY_FILE}
    for bad in ("..\\..\\backend\\main.py", "../.env", "demo_state.json",
                "collector.log", "C:\\Windows\\win.ini"):
        assert bad not in known
    assert CATEGORIES["RPO"]["file"] in known
    print("  download whitelist OK")

    print("\nALL SECURITY TESTS PASSED")


if __name__ == "__main__":
    main()
