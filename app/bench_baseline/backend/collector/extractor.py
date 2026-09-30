"""HTML data extraction: JSON-LD, mailto/tel links, visible text, meta tags.
Ported from business_collector.py (bounded regex scans included — unbounded
patterns previously froze the process on pathological pages).
Nothing is fabricated: missing values stay blank."""

from __future__ import annotations

import json
import re
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .normalize import clean_text, norm_domain

MAX_SCAN_CHARS = 200_000

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,4}\.[A-Za-z]{2,12}"
)
BAD_EMAIL_PARTS = (
    "example.", "sentry", "wixpress", "domain.com", "email.com", "yourdomain",
    "@2x", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "schema.org",
    "w3.org", "noreply", "no-reply", "donotreply", "@sentry", "your@",
    "name@", "user@", "@test.", "@localhost", "jquery", "bootstrap",
)
PHONE_RE = re.compile(
    r"(?<![\w/#])(?:\+\s?\d{1,3}[\s.\-]?)?(?:\(\s?\d{1,5}\s?\)|\d{1,5})"
    r"(?:[\s.\-]?\d{2,5}){2,4}(?![\w])"
)
DATE_LIKE_RE = re.compile(r"^\d{1,4}[./\-\s]+\d{1,2}[./\-\s]+\d{1,4}$")
YEAR_RANGE_RE = re.compile(r"^(19|20)\d{2}\s*[-–.]\s*(19|20)\d{2}$")
PHONE_CONTEXT_RE = re.compile(
    r"tel|phone|call|mob|whatsapp|contact|ph\b|dial|hotline|toll", re.I
)
ADDR_CLASS_RE = re.compile(r"(^|[\s_\-])(address|addr|location|office)([\s_\-]|$)", re.I)
ADDR_WORD_RE = re.compile(
    r"\b(street|st\.|road|rd\.|avenue|ave\.?|suite|ste\.|floor|fl\.|building|"
    r"bldg|blvd|boulevard|lane|ln\.|drive|dr\.|plaza|tower|towers|sector|nagar|"
    r"park|way|square|centre|center|complex|house|block|estate|unit|level|"
    r"po box|p\.o\. box|highway|hwy|court|ct\.|place|pl\.|marg|colony|"
    r"industrial|business park|tech park)\b", re.I,
)
SERVICE_HREF_RE = re.compile(
    r"service|solution|practice|expertise|what-we-do|offering|capabilit|"
    r"specialt|industr|product|portfolio", re.I
)
GENERIC_LINK_TEXT = {
    "services", "our services", "solutions", "our solutions", "read more",
    "learn more", "view all", "more", "home", "about", "about us", "contact",
    "contact us", "products", "industries", "view more", "see all", "explore",
    "get started", "click here", "here", "menu", "login", "sign in",
}
GENERIC_TITLE_PARTS = {
    "home", "homepage", "home page", "welcome", "index", "official site",
    "official website", "main page", "start", "untitled", "contact",
    "contact us", "about", "about us", "services", "our services",
}

ORG_TYPE_HINTS = (
    "organization", "localbusiness", "corporation", "professionalservice",
    "legalservice", "financialservice", "medicalorganization",
    "medicalbusiness", "employmentagency", "accountingservice", "dentist",
    "physician", "hospital", "store", "attorney", "insuranceagency",
    "homeandconstructionbusiness", "healthandbeautybusiness",
)


class Extracted:
    __slots__ = ("name", "email", "phone", "address", "services")

    def __init__(self):
        self.name = ""
        self.email: list[str] = []
        self.phone: list[str] = []
        self.address = ""
        self.services = ""

    def merge_missing(self, other: "Extracted"):
        for e in other.email:
            if e not in self.email and len(self.email) < 3:
                self.email.append(e)
        for p in other.phone:
            if p not in self.phone and len(self.phone) < 2:
                self.phone.append(p)
        if not self.address:
            self.address = other.address
        if not self.services:
            self.services = other.services
        if not self.name:
            self.name = other.name


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name})
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def _iter_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from _iter_ld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "publisher", "mainEntity", "itemListElement",
                    "item", "provider", "author"):
            if key in node:
                yield from _iter_ld(node[key])


def _format_ld_address(addr) -> str:
    if isinstance(addr, list):
        addr = addr[0] if addr else ""
    if isinstance(addr, str):
        return clean_text(addr)
    if isinstance(addr, dict):
        parts = []
        for key in ("streetAddress", "addressLocality", "addressRegion",
                    "postalCode", "addressCountry"):
            v = addr.get(key)
            if isinstance(v, dict):
                v = v.get("name")
            if v:
                parts.append(clean_text(str(v)))
        return ", ".join(parts)
    return ""


def parse_json_ld(soup: BeautifulSoup) -> dict:
    out: dict = {}
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for obj in _iter_ld(data):
            t = obj.get("@type")
            types = [str(x).lower() for x in (t if isinstance(t, list) else [t]) if x]
            if "website" in types and obj.get("name") and not out.get("site_name"):
                out["site_name"] = clean_text(str(obj["name"]))
            if not any(any(h in x for h in ORG_TYPE_HINTS) for x in types):
                continue
            if obj.get("name") and not out.get("name"):
                out["name"] = clean_text(str(obj.get("name")))
            if obj.get("legalName") and not out.get("name"):
                out["name"] = clean_text(str(obj.get("legalName")))
            if obj.get("email") and not out.get("email"):
                out["email"] = clean_text(str(obj["email"]).replace("mailto:", "")).lower()
            if obj.get("telephone") and not out.get("phone"):
                tel = obj["telephone"]
                out["phone"] = clean_text(str(tel[0] if isinstance(tel, list) else tel))
            if obj.get("address") and not out.get("address"):
                out["address"] = _format_ld_address(obj["address"])
            if obj.get("description") and not out.get("description"):
                out["description"] = clean_text(str(obj["description"]))
    return out


def clean_title(title: str) -> str:
    title = clean_text(title)
    if not title:
        return ""
    parts = re.split(r"\s+[\|\-–—:•·»«/]\s+|\s+[\|•·»«]\s*|\s*[\|•·»«]\s+", title)
    strip_chars = " -|:•·»"
    cands = [p.strip(strip_chars) for p in parts if p and p.strip(strip_chars)]
    cands = [c for c in cands if c.lower() not in GENERIC_TITLE_PARTS and len(c) >= 2]
    if not cands:
        return ""
    # Titles conventionally lead with the brand ("Acme Corp | tagline"), so
    # prefer the first non-generic part (the old shortest-part rule kept
    # returning taglines instead of company names).
    return cands[0][:120]


def extract_emails(soup: BeautifulSoup, text: str, raw_html: str) -> list[str]:
    found: list[str] = []
    for a in soup.select('a[href^="mailto:"]'):
        addr = unquote(a.get("href", "")[7:].split("?")[0])
        found.extend(EMAIL_RE.findall(addr[:500]))
    found.extend(EMAIL_RE.findall(text[:MAX_SCAN_CHARS]))
    found.extend(EMAIL_RE.findall(raw_html[:MAX_SCAN_CHARS]))
    out: list[str] = []
    for e in found:
        e = e.strip(".-_").lower()
        if len(e) > 80 or any(b in e for b in BAD_EMAIL_PARTS):
            continue
        if e not in out:
            out.append(e)
        if len(out) >= 3:
            break
    return out


def _valid_phone(cand: str) -> str:
    cand = clean_text(cand)
    digits = re.sub(r"\D", "", cand)
    if not 8 <= len(digits) <= 15:
        return ""
    if len(set(digits)) <= 2:
        return ""
    if DATE_LIKE_RE.match(cand) or YEAR_RANGE_RE.match(cand):
        return ""
    if digits in ("1234567890", "0123456789", "9876543210"):
        return ""
    return cand


def extract_phones(soup: BeautifulSoup, text: str) -> list[str]:
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    def push(cand: str, score: int):
        v = _valid_phone(cand)
        if not v:
            return
        key = re.sub(r"\D", "", v)
        if key in seen:
            return
        seen.add(key)
        scored.append((score, v))

    for a in soup.select('a[href^="tel:"]'):
        push(unquote(a.get("href", "")[4:]).replace("-", " ").strip(), 10)
    text = text[:MAX_SCAN_CHARS]
    for m in PHONE_RE.finditer(text):
        ctx = text[max(0, m.start() - 30):m.start()]
        score = 0
        if PHONE_CONTEXT_RE.search(ctx):
            score += 3
        if m.group(0).strip().startswith("+"):
            score += 2
        if "(" in m.group(0):
            score += 1
        push(m.group(0), score)
    scored.sort(key=lambda t: -t[0])
    return [v for _, v in scored[:2]]


def extract_address(soup: BeautifulSoup, text: str) -> str:
    def ok(t: str) -> bool:
        return 10 <= len(t) <= 250 and any(ch.isdigit() for ch in t)

    for tag in soup.find_all("address"):
        t = clean_text(tag.get_text(" "))
        if 10 <= len(t) <= 250:
            return t
    for el in soup.find_all(attrs={"itemprop": "address"}):
        t = clean_text(el.get_text(" "))
        if ok(t):
            return t
    for el in soup.find_all(class_=ADDR_CLASS_RE)[:20] + soup.find_all(id=ADDR_CLASS_RE)[:10]:
        t = clean_text(el.get_text(" "))
        if ok(t) and "@" not in t and ADDR_WORD_RE.search(t):
            return t
    for line in text.splitlines():
        line = clean_text(line)
        if 15 <= len(line) <= 200 and line.count(",") >= 2 and ok(line) and ADDR_WORD_RE.search(line):
            return line
    return ""


def build_services(soup: BeautifulSoup, ld_desc: str) -> str:
    desc = clean_text(ld_desc or meta_content(soup, "description", "og:description"))[:220]
    items: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        txt = clean_text(a.get_text(" "))
        if 3 <= len(txt) <= 45 and SERVICE_HREF_RE.search(href) and txt.lower() not in GENERIC_LINK_TEXT:
            if txt not in items:
                items.append(txt)
        if len(items) >= 10:
            break
    if len(items) < 3:
        for h in soup.find_all(["h2", "h3"]):
            txt = clean_text(h.get_text(" "))
            if 3 <= len(txt) <= 60 and txt.lower() not in GENERIC_LINK_TEXT and txt not in items:
                items.append(txt)
            if len(items) >= 8:
                break
    parts = []
    if desc:
        parts.append(desc)
    if items:
        parts.append("Services: " + ", ".join(items))
    return " | ".join(parts)[:500]


def extract_company(soup: BeautifulSoup, raw_html: str, url: str) -> Extracted:
    ex = Extracted()
    ld = parse_json_ld(soup)
    title = soup.title.get_text() if soup.title else ""
    ex.name = (
        ld.get("name") or meta_content(soup, "og:site_name") or ld.get("site_name")
        or clean_title(title) or norm_domain(url)
    )[:150]
    text = soup.get_text("\n")[:MAX_SCAN_CHARS]
    ex.email = extract_emails(soup, text, raw_html)
    if ld.get("email") and ld["email"] not in ex.email:
        ex.email.insert(0, ld["email"])
    ex.phone = extract_phones(soup, text)
    if ld.get("phone"):
        v = _valid_phone(ld["phone"])
        if v and v not in ex.phone:
            ex.phone.insert(0, v)
    ex.address = ld.get("address") or extract_address(soup, text)
    ex.services = build_services(soup, ld.get("description", ""))
    return ex


# --------------------------------------------------------------------------- #
# City / State / Country parsing (best effort, blank when unsure)
# --------------------------------------------------------------------------- #

COUNTRIES = {
    "india", "usa", "united states", "united states of america", "uk",
    "united kingdom", "canada", "australia", "uae", "united arab emirates",
    "singapore", "ireland", "germany", "netherlands", "south africa",
    "new zealand", "philippines", "malaysia", "hong kong", "switzerland",
    "france", "spain", "italy", "sweden", "poland", "saudi arabia", "qatar",
    "nigeria", "kenya", "japan", "china", "indonesia", "thailand", "brazil",
    "mexico", "belgium", "austria", "denmark", "norway", "finland", "portugal",
}
_POSTAL_RE = re.compile(r"\b\d{4,7}(?:[-\s]\d{3,4})?\b")


def split_address(address: str, location_hint: str) -> tuple[str, str, str]:
    """Return (city, state, country) parsed from a comma-separated address.
    location_hint (the user's location input) supplies the country when the
    address itself doesn't name one. Unknown parts stay blank."""
    city = state = country = ""
    hint = clean_text(location_hint)
    if hint.lower() in COUNTRIES:
        country = hint
    parts = [clean_text(p) for p in (address or "").split(",") if clean_text(p)]
    if parts:
        last = parts[-1]
        if last.lower() in COUNTRIES:
            country = last
            parts = parts[:-1]
        if parts:
            tail = _POSTAL_RE.sub("", parts[-1]).strip()
            if tail and not any(ch.isdigit() for ch in tail) and len(tail) <= 40:
                state = tail
                parts = parts[:-1]
        if parts:
            cand = _POSTAL_RE.sub("", parts[-1]).strip()
            if cand and not any(ch.isdigit() for ch in cand) and len(cand) <= 40:
                city = cand
    if hint and not country and not state and not city:
        # No parsable address: fall back to the location the user searched in.
        if hint.lower() in COUNTRIES:
            country = hint
        else:
            city = hint
    return city, state, country
