"""Normalization helpers shared by extraction, validation and deduplication.
Ported from business_collector.py."""

from __future__ import annotations

import re
from urllib.parse import urlparse

ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|llc|l\.l\.c|inc|incorporated|llp|l\.l\.p|pvt|private|plc|"
    r"corp|corporation|co|company|gmbh|ag|sa|s\.a|pte|sdn|bhd|bv|b\.v|nv|"
    r"pty|pllc|pc|p\.c|lp|group|holdings?)\b\.?", re.I,
)
MULTI_PART_SECOND_LEVEL = {
    "co", "com", "org", "net", "gov", "ac", "edu", "gen", "firm", "ltd",
    "plc", "nic", "or", "ne", "biz", "info",
}


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = ILLEGAL_XLSX_RE.sub("", str(s))
    return re.sub(r"\s+", " ", s).strip()


def norm_domain(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    netloc = urlparse(url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def root_domain(url: str) -> str:
    d = norm_domain(url)
    parts = [p for p in d.split(".") if p]
    if len(parts) >= 3 and parts[-2] in MULTI_PART_SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return d


def norm_name(name: str) -> str:
    n = LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n if len(n) >= 4 else ""


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 8 else ""


def xlsx_safe(value) -> str:
    s = clean_text(value)
    if s[:1] in ("=", "+", "@"):
        s = " " + s
    return s[:32000]
