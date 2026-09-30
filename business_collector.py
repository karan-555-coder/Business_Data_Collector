#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
business_collector.py
=====================

Pure-Python (no frameworks) collector of public business data.

For each category it runs a fixed list of search queries against a search
engine (Serper.dev Google API when a key is available, otherwise the
DuckDuckGo HTML endpoint with Bing as fallback), crawls the top N results per
query, extracts company details from each site (home page plus up to two
contact/about pages), de-duplicates on Company Name + Website, and keeps
searching until the requested number of UNIQUE records is reached (or every
query has been exhausted / the time budget is spent).

With a Serper key the collector additionally queries the Google Places
endpoint for every query: each place is a structured business record (name,
address, phone, website, business category). Its website is then crawled to
fill in email and services.

Serper key lookup order: --serper-key, SERPER_API_KEY env var, serper_key.txt
next to this script.

Outputs (in --out directory, default ./output_data):
    00_Master_Summary.xlsx          statistics
    01_Finance.xlsx ... 10_Other_B2B.xlsx   one file per category
    collector_state.json            checkpoint (re-run resumes automatically)
    collector.log                   run log

Install dependencies:
    pip install requests beautifulsoup4 pandas openpyxl

Run:
    python business_collector.py                       # target 15,000 records
    python business_collector.py --target 500 --max-minutes 30
    python business_collector.py --fresh               # ignore old checkpoint

Notes on reaching 15,000 records
--------------------------------
The 34 base queries x 3 results = ~100 pages, which can never yield 15,000
companies. The script therefore (a) uses the exact base queries first, then
(b) re-issues them with location modifiers ("... in London", "... in Mumbai",
...) and (c) harvests outbound company links from directory / listicle pages
that appear in the results. Search engines throttle automated traffic, so a
full 15,000-record run takes many hours; the script paces itself, backs off
when throttled, switches engines, checkpoints progress and can be resumed.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib import robotparser
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

CATEGORIES: "OrderedDict[str, list[str]]" = OrderedDict([
    ("Finance", [
        "finance companies", "financial advisory firms",
        "corporate finance firms", "investment advisory firms",
    ]),
    ("CDS_Corporate_Compliance", [
        "company secretarial services", "corporate secretarial firms",
        "corporate compliance firms",
    ]),
    ("Forms", [
        "business forms services", "corporate filing services",
        "company filing services",
    ]),
    ("Advisory", [
        "business advisory firms", "management consulting firms",
        "strategy consulting",
    ]),
    ("Law_Firms", [
        "corporate law firms", "business law firms", "legal advisory firms",
    ]),
    ("Recruitment", [
        "recruitment agencies", "staffing companies", "executive search firms",
    ]),
    ("RPO", [
        "RPO companies", "recruitment process outsourcing", "RPO providers",
    ]),
    ("Medical_Healthcare", [
        "medical companies", "healthcare companies", "medical service providers",
    ]),
    ("3D_Studios", [
        "3D studios", "3D visualization studios", "3D rendering companies",
    ]),
    ("Other_B2B", [
        "B2B service providers", "business solutions companies",
        "industrial services firms",
    ]),
])

FILE_PREFIX = {cat: f"{i:02d}" for i, cat in enumerate(CATEGORIES, start=1)}

# Used only after the exact base queries are exhausted, to widen the search.
LOCATION_MODIFIERS = [
    "in USA", "in UK", "in India", "in Canada", "in Australia", "in UAE",
    "in Singapore", "in Ireland", "in Germany", "in Netherlands",
    "in South Africa", "in New Zealand", "in Philippines", "in Malaysia",
    "in Hong Kong", "in Switzerland", "in France", "in Spain", "in Italy",
    "in Sweden", "in Poland", "in Saudi Arabia", "in Qatar", "in Nigeria",
    "in Kenya",
    "in London", "in New York", "in Chicago", "in Los Angeles", "in Houston",
    "in Dallas", "in Atlanta", "in Boston", "in San Francisco", "in Seattle",
    "in Miami", "in Denver", "in Phoenix", "in Toronto", "in Vancouver",
    "in Sydney", "in Melbourne", "in Brisbane", "in Dubai", "in Abu Dhabi",
    "in Mumbai", "in Delhi", "in Bangalore", "in Hyderabad", "in Chennai",
    "in Pune", "in Kolkata", "in Ahmedabad", "in Gurgaon", "in Noida",
    "in Manchester", "in Birmingham", "in Leeds", "in Glasgow", "in Edinburgh",
    "in Dublin", "in Amsterdam", "in Berlin", "in Frankfurt", "in Munich",
    "in Paris", "in Madrid", "in Barcelona", "in Milan", "in Zurich",
    "in Stockholm", "in Warsaw", "in Johannesburg", "in Cape Town",
    "in Auckland", "in Manila", "in Kuala Lumpur", "in Jakarta", "in Bangkok",
    "in Tokyo", "in Riyadh", "in Doha", "in Lagos", "in Nairobi",
    "near me", "list", "top", "best", "directory",
]

# Domains that are never treated as a company record (search engines, social
# networks, encyclopedias, marketplaces, directories). Directory-like pages
# are still fetched so their outbound company links can be harvested.
SKIP_DOMAINS = {
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "instagram.com",
    "youtube.com", "tiktok.com", "pinterest.com", "reddit.com", "quora.com",
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "britannica.com",
    "amazon.com", "apple.com", "microsoft.com", "adobe.com", "wordpress.com",
    "wordpress.org", "blogspot.com", "tumblr.com", "medium.com", "github.com",
    "glassdoor.com", "indeed.com", "ambitionbox.com", "naukri.com",
    "monster.com", "ziprecruiter.com", "simplyhired.com",
    "yelp.com", "yellowpages.com", "bbb.org", "crunchbase.com",
    "zoominfo.com", "dnb.com", "bloomberg.com", "forbes.com", "inc.com",
    "investopedia.com", "statista.com", "nerdwallet.com", "bankrate.com",
    "clutch.co", "goodfirms.co", "designrush.com", "sortlist.com",
    "themanifest.com", "expertise.com", "upcity.com", "g2.com",
    "capterra.com", "trustpilot.com", "justdial.com", "sulekha.com",
    "indiamart.com", "tradeindia.com", "europages.com", "kompass.com",
    "thomasnet.com", "manta.com", "chamberofcommerce.com", "cylex.com",
    "hotfrog.com", "brownbook.net", "yell.com", "thomsonlocal.com",
    "lawyers.com", "avvo.com", "findlaw.com", "justia.com",
    "martindale.com", "legal500.com", "chambers.com", "superlawyers.com",
    "healthgrades.com", "webmd.com", "zocdoc.com", "vitals.com", "practo.com",
    "behance.net", "artstation.com", "dribbble.com", "vimeo.com",
    "wikihow.com", "linktr.ee", "archive.org", "sec.gov", "gov.uk",
    "cloudflare.com", "godaddy.com", "wix.com", "squarespace.com",
    "shopify.com", "hubspot.com", "mailchimp.com", "salesforce.com",
    "gartner.com", "mckinsey.com", "coursera.org", "udemy.com",
    # news / media / rankings that kept showing up as "companies"
    "fortune.com", "builtin.com", "fintechmagazine.com", "wikimediafoundation.org",
    "prospects.ac.uk", "bcgsearch.com", "businessinsider.com", "cnbc.com",
    "reuters.com", "wsj.com", "nytimes.com", "theguardian.com", "ft.com",
    "economictimes.com", "indiatimes.com", "moneycontrol.com", "livemint.com",
    "techcrunch.com", "entrepreneur.com", "hbr.org", "vault.com", "fool.com",
    "marketwatch.com", "msn.com", "cnn.com", "bbc.com", "bbc.co.uk",
    "thebalancemoney.com", "smartasset.com", "zippia.com", "comparably.com",
    "owler.com", "cbinsights.com", "pitchbook.com", "tracxn.com", "f6s.com",
    "startupranking.com", "growjo.com", "rocketreach.co", "apollo.io",
    "signalhire.com", "lusha.com", "sciencedirect.com", "researchgate.net",
    "springer.com", "nature.com", "ncbi.nlm.nih.gov", "who.int", "un.org",
    "worldbank.org", "imf.org", "oecd.org", "europa.eu", "ebay.com",
    "alibaba.com", "aliexpress.com", "etsy.com", "walmart.com", "target.com",
    "craigslist.org", "gumtree.com", "olx.in", "99acres.com", "magicbricks.com",
}

# Host names that are almost always publishers/aggregators rather than firms.
MEDIA_DOMAIN_RE = re.compile(
    r"(^|[.\-])(news|magazine|mag|wiki|forum|forums|tribune|gazette|herald|"
    r"journal|times|daily|weekly|press|insider|ranking|rankings|top10|"
    r"toplist|listing|listings|directory|awards)([.\-]|$)", re.I,
)
MEDIA_NAME_RE = re.compile(
    r"\b(magazine|news|forum|wiki|wikipedia|foundation|university|college|"
    r"institute of technology|tribune|gazette|herald|journal|times|encyclopedia|"
    r"dictionary|top \d+|best \d+|\d+ best|rankings?)\b", re.I,
)

# Pages on these hosts are always mined for outbound company links.
AGGREGATOR_DOMAINS = {
    "clutch.co", "goodfirms.co", "designrush.com", "sortlist.com",
    "themanifest.com", "expertise.com", "upcity.com", "g2.com", "capterra.com",
    "yelp.com", "yellowpages.com", "justdial.com", "sulekha.com",
    "indiamart.com", "europages.com", "kompass.com", "thomasnet.com",
    "manta.com", "hotfrog.com", "cylex.com", "yell.com", "legal500.com",
    "chambers.com", "findlaw.com", "martindale.com", "superlawyers.com",
    "avvo.com", "healthgrades.com", "behance.net", "artstation.com",
    "wikipedia.org", "forbes.com", "inc.com", "crunchbase.com", "bbb.org",
    "trustpilot.com", "medium.com", "reddit.com", "quora.com", "glassdoor.com",
}

# Minimum number of distinct external domains for a non-aggregator page to be
# treated as a "list" page worth mining for company links.
LIST_PAGE_MIN_EXTERNAL_LINKS = 12
MAX_CANDIDATES_PER_PAGE = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

COLUMNS = [
    "Company Name", "Website", "Email", "Phone", "Address", "Services",
    "Category", "Source Query", "Source URL", "Collected At",
]

# --------------------------------------------------------------------------- #
# Regexes and helpers
# --------------------------------------------------------------------------- #

# Bounded quantifiers: the earlier unbounded pattern backtracked for minutes
# on pathological pages (megabytes of base64 with stray '@'), freezing the
# whole process because the regex engine holds the GIL.
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,4}\.[A-Za-z]{2,12}"
)
# Never feed more than this many characters into findall/finditer scans.
MAX_SCAN_CHARS = 200_000
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
CONTACT_LINK_RE = re.compile(
    r"contact|about|reach-?us|get-?in-?touch|locations?|offices?|find-?us|"
    r"visit-?us", re.I
)
# Titles of bot-challenge / error pages: never record these as companies.
BLOCK_TITLE_RE = re.compile(
    r"client challenge|just a moment|attention required|access denied|"
    r"are you a human|verify you are|captcha|403 forbidden|404|not found|"
    r"page not found|error \d{3}|service unavailable|site not found|"
    r"domain (?:is )?for sale|parked domain|coming soon|under construction|"
    r"account suspended|bot detection|security check|please wait", re.I,
)
GENERIC_TITLE_PARTS = {
    "home", "homepage", "home page", "welcome", "index", "official site",
    "official website", "main page", "start", "untitled",
}
LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|llc|l\.l\.c|inc|incorporated|llp|l\.l\.p|pvt|private|plc|"
    r"corp|corporation|co|company|gmbh|ag|sa|s\.a|pte|sdn|bhd|bv|b\.v|nv|"
    r"pty|pllc|pc|p\.c|lp|group|holdings?)\b\.?", re.I,
)
ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
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


def is_skip_domain(url: str) -> bool:
    rd = root_domain(url)
    if not rd:
        return True
    if rd in SKIP_DOMAINS:
        return True
    if rd.endswith((".gov", ".edu", ".mil")) or ".gov." in rd or ".edu." in rd or ".ac." in rd:
        return True
    if MEDIA_DOMAIN_RE.search(rd.rsplit(".", 1)[0]):
        return True
    return False


def is_aggregator(url: str) -> bool:
    return root_domain(url) in AGGREGATOR_DOMAINS


def norm_name(name: str) -> str:
    n = LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n if len(n) >= 4 else ""


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def xlsx_safe(value) -> str:
    s = clean_text(value)
    if s[:1] in ("=", "+", "@"):
        s = " " + s
    return s[:32000]


# --------------------------------------------------------------------------- #
# HTTP session / robots
# --------------------------------------------------------------------------- #

def make_session(verify: bool = True, retries: int = 2) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries, connect=retries, read=retries, backoff_factor=0.6,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        # Never honor a server-sent Retry-After header: hostile/broken sites
        # send hours-long values and freeze the worker thread (this hung the
        # 2026-09-28 run for over an hour).
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    s.verify = verify
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


class RobotsCache:
    """Per-host robots.txt cache. Unreachable/missing robots.txt => allowed."""

    DEAD = "dead"  # host did not answer at all; do not waste time fetching pages

    def __init__(self, session: requests.Session):
        self.session = session
        self.cache: dict[str, object] = {}
        self.lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self.lock:
            have = base in self.cache
            rp = self.cache.get(base)
        if not have:
            rp = None
            try:
                r = self.session.get(base + "/robots.txt", timeout=(6, 10))
                if r.status_code == 200 and "html" not in r.headers.get("Content-Type", "").lower():
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except (requests.ConnectionError, requests.Timeout):
                rp = self.DEAD
            except Exception:
                rp = None
            with self.lock:
                self.cache[base] = rp
        if rp == self.DEAD:
            return False
        if rp is None:
            return True
        try:
            return rp.can_fetch("*", url)
        except Exception:
            return True


# --------------------------------------------------------------------------- #
# Search engines
# --------------------------------------------------------------------------- #

class SearchBlocked(Exception):
    pass


class SerperDisabled(Exception):
    """Serper key invalid or out of credits: stop using it for this run."""


class Serper:
    """Thin client for the Serper.dev Google Search / Places API.

    Both endpoints return 10 rows per page on the standard plan and charge one
    credit per page, so callers paginate with `page`.
    """

    SEARCH_URL = "https://google.serper.dev/search"
    PLACES_URL = "https://google.serper.dev/places"

    def __init__(self, api_key: str, verify: bool = True):
        self.api_key = api_key
        self.session = make_session(verify, retries=1)
        self.session.headers.update({"X-API-KEY": api_key, "Content-Type": "application/json"})
        self.credits_used = 0
        self.disabled_reason = ""
        self.lock = threading.Lock()
        self.log = logging.getLogger("serper")

    @property
    def enabled(self) -> bool:
        return not self.disabled_reason

    def _post(self, url: str, payload: dict) -> dict:
        if self.disabled_reason:
            raise SerperDisabled(self.disabled_reason)
        for attempt in range(3):
            resp = self.session.post(url, json=payload, timeout=(10, 40))
            if resp.status_code == 200:
                data = resp.json()
                with self.lock:
                    self.credits_used += int(data.get("credits", 1) or 1)
                return data
            body = resp.text[:200]
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                self.log.warning("serper rate-limited (429); waiting %ds", wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403) or "credit" in body.lower():
                self.disabled_reason = f"serper HTTP {resp.status_code}: {body}"
                self.log.error("%s -- falling back to public engines", self.disabled_reason)
                raise SerperDisabled(self.disabled_reason)
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            raise SearchBlocked(f"serper HTTP {resp.status_code}: {body}")
        raise SearchBlocked("serper: repeated 429/5xx")

    def search(self, query: str, max_results: int) -> list[str]:
        urls: list[str] = []
        page = 1
        while len(urls) < max_results and page <= 10:
            data = self._post(self.SEARCH_URL, {"q": query, "num": 10, "page": page})
            organic = data.get("organic") or []
            if not organic:
                break
            for item in organic:
                link = item.get("link") or ""
                if link.startswith("http") and link not in urls:
                    urls.append(link)
            # Also harvest "sitelinks" - usually deeper pages of the same site,
            # harmless because the crawler de-duplicates per domain.
            page += 1
        return urls[:max_results]

    def places(self, query: str, pages: int) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            data = self._post(self.PLACES_URL, {"q": query, "page": page})
            rows = data.get("places") or []
            if not rows:
                break
            for p in rows:
                key = str(p.get("cid") or p.get("placeId") or p.get("title"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(p)
            if len(rows) < 10:
                break
        return out


class SearchEngines:
    """Serper (Google) first when a key is present, then DuckDuckGo HTML
    endpoint, then Bing, with pacing/back-off for the public engines."""

    def __init__(self, delay: float, preference: str = "auto", verify: bool = True,
                 serper: "Serper | None" = None):
        self.session = make_session(verify)
        self.delay = delay
        self.preference = preference
        self.serper = serper
        self.lock = threading.Lock()
        self.cooldown_until = {"serper": 0.0, "ddg": 0.0, "bing": 0.0}
        self.block_count = {"serper": 0, "ddg": 0, "bing": 0}
        self.requests_made = 0
        self.log = logging.getLogger("search")

    # -- pacing -------------------------------------------------------------
    def _pace(self, engine: str = "ddg"):
        if engine != "serper":
            time.sleep(self.delay + random.uniform(0, self.delay))
        self.requests_made += 1

    def _engine_order(self) -> list[str]:
        if self.preference in ("ddg", "bing"):
            return [self.preference]
        have_serper = self.serper is not None and self.serper.enabled
        if self.preference == "serper":
            return ["serper"] if have_serper else ["bing", "ddg"]
        order = ["ddg", "bing"]
        now = time.time()
        order = sorted(order, key=lambda e: (self.cooldown_until[e] > now, self.block_count[e]))
        if have_serper:
            order.insert(0, "serper")
        return order

    def _mark_blocked(self, engine: str):
        self.block_count[engine] += 1
        wait = min(600, 30 * (2 ** (self.block_count[engine] - 1)))
        self.cooldown_until[engine] = time.time() + wait
        self.log.warning("%s throttled/unreachable; cooling down for %ds", engine, wait)

    # -- public -------------------------------------------------------------
    def search(self, query: str, max_results: int) -> list[str]:
        # Serper needs no pacing and is safe to call from many threads at once,
        # so it runs outside the lock that serialises the public engines.
        if self.serper is not None and self.serper.enabled and self.preference in ("auto", "serper"):
            try:
                self.requests_made += 1
                return self.serper.search(query, max_results)[:max_results]
            except SerperDisabled:
                pass
            except (SearchBlocked, requests.RequestException) as exc:
                self.log.warning("serper failed for %r: %s", query, str(exc)[:160])
                return []
            if self.preference == "serper":
                return []
        with self.lock:
            for engine in self._engine_order():
                if engine == "serper":
                    continue
                wait = self.cooldown_until[engine] - time.time()
                if wait > 0:
                    if len(self._engine_order()) == 1 or all(
                        self.cooldown_until[e] > time.time() for e in self.cooldown_until
                    ):
                        self.log.info("all engines cooling down; sleeping %ds", int(wait) + 1)
                        time.sleep(wait + 1)
                    else:
                        continue
                try:
                    self._pace(engine)
                    if engine == "serper":
                        urls = self.serper.search(query, max_results)
                    elif engine == "ddg":
                        urls = self._ddg(query, max_results)
                    else:
                        urls = self._bing(query, max_results)
                    self.block_count[engine] = max(0, self.block_count[engine] - 1) if urls else self.block_count[engine]
                    return urls[:max_results]
                except SerperDisabled:
                    continue
                except SearchBlocked:
                    self._mark_blocked(engine)
                except requests.RequestException as exc:
                    self.log.warning("%s unreachable for %r: %s", engine, query, str(exc)[:160])
                    self._mark_blocked(engine)
            return []

    def places(self, query: str, pages: int) -> list[dict]:
        """Structured Google Places rows for the query (empty without Serper)."""
        if self.serper is None or not self.serper.enabled or pages <= 0:
            return []
        try:
            return self.serper.places(query, pages)
        except (SerperDisabled, SearchBlocked) as exc:
            self.log.warning("places lookup failed for %r: %s", query, exc)
            return []
        except requests.RequestException as exc:
            self.log.warning("places unreachable for %r: %s", query, str(exc)[:160])
            return []

    # -- DuckDuckGo ---------------------------------------------------------
    @staticmethod
    def _ddg_unwrap(href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        if "duckduckgo.com/l/" in href:
            q = parse_qs(urlparse(href).query)
            return unquote(q.get("uddg", [""])[0])
        if href.startswith("http") and "duckduckgo.com" not in href:
            return href
        return ""

    def _ddg(self, query: str, max_results: int) -> list[str]:
        url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "b": "", "kl": "wt-wt"}
        results: list[str] = []
        pages = 0
        while len(results) < max_results and pages < 3:
            resp = self.session.post(
                url, data=data, timeout=(10, 25),
                headers={"Referer": "https://html.duckduckgo.com/"},
            )
            if resp.status_code in (403, 429) or "anomaly" in resp.url:
                raise SearchBlocked(f"ddg status {resp.status_code}")
            low = resp.text.lower()
            if "bots use duckduckgo too" in low or "unusual traffic" in low:
                raise SearchBlocked("ddg captcha page")
            soup = BeautifulSoup(resp.text, "html.parser")
            found_any = False
            for div in soup.select("div.result"):
                cls = " ".join(div.get("class", []))
                if "result--ad" in cls or "result--no-result" in cls:
                    continue
                a = div.select_one("a.result__a")
                if not a:
                    continue
                real = self._ddg_unwrap(a.get("href", ""))
                if real and real not in results:
                    results.append(real)
                    found_any = True
            if not found_any:
                break
            nxt = None
            for form in soup.find_all("form"):
                if form.find("input", {"value": "Next"}) is not None:
                    nxt = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
                    break
            if not nxt or len(results) >= max_results:
                break
            data = nxt
            pages += 1
            self._pace()
        return results

    # -- Bing ---------------------------------------------------------------
    @staticmethod
    def _bing_unwrap(href: str) -> str:
        if "bing.com/ck/a" in href:
            u = parse_qs(urlparse(href).query).get("u", [""])[0]
            if u.startswith("a1"):
                u = u[2:]
            try:
                pad = "=" * (-len(u) % 4)
                return base64.urlsafe_b64decode(u + pad).decode("utf-8", "replace")
            except Exception:
                return ""
        return href if href.startswith("http") else ""

    def _bing(self, query: str, max_results: int) -> list[str]:
        results: list[str] = []
        first = 1
        while len(results) < max_results and first <= 31:
            resp = self.session.get(
                "https://www.bing.com/search",
                params={"q": query, "first": first, "count": 10, "setlang": "en"},
                timeout=(10, 25),
            )
            if resp.status_code in (403, 429):
                raise SearchBlocked(f"bing status {resp.status_code}")
            soup = BeautifulSoup(resp.text, "html.parser")
            title = (soup.title.get_text() if soup.title else "").lower()
            if "captcha" in title or "unusual traffic" in resp.text.lower():
                raise SearchBlocked("bing captcha")
            items = soup.select("li.b_algo h2 a")
            if not items:
                break
            for a in items:
                real = self._bing_unwrap(a.get("href", ""))
                if real and "bing.com" not in real and real not in results:
                    results.append(real)
            first += 10
            if len(results) < max_results:
                self._pace()
        return results


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

ORG_TYPE_HINTS = (
    "organization", "localbusiness", "corporation", "professionalservice",
    "legalservice", "financialservice", "medicalorganization",
    "medicalbusiness", "employmentagency", "accountingservice", "dentist",
    "physician", "hospital", "store", "attorney", "insuranceagency",
    "homeandconstructionbusiness", "healthandbeautybusiness",
)


def _iter_ld(node):
    if isinstance(node, list):
        for item in node:
            yield from _iter_ld(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "publisher", "mainEntity", "itemListElement", "item", "provider", "author"):
            if key in node:
                yield from _iter_ld(node[key])


def _format_ld_address(addr) -> str:
    if isinstance(addr, list):
        addr = addr[0] if addr else ""
    if isinstance(addr, str):
        return clean_text(addr)
    if isinstance(addr, dict):
        parts = []
        for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
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
    cands.sort(key=len)
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


def find_contact_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    base_root = root_domain(base_url)
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        txt = a.get_text(" ")
        if CONTACT_LINK_RE.search(href) or CONTACT_LINK_RE.search(txt):
            full = urljoin(base_url, href).split("#")[0]
            if not full.lower().startswith("http"):
                continue
            if root_domain(full) != base_root:
                continue
            if full.rstrip("/") == base_url.rstrip("/") or full in out:
                continue
            if re.search(r"\.(pdf|jpg|png|zip|docx?)$", full, re.I):
                continue
            out.append(full)
    out.sort(key=lambda u: 0 if "contact" in u.lower() else 1)
    return out


def external_links(soup: BeautifulSoup, page_url: str, cap: int = MAX_CANDIDATES_PER_PAGE) -> list[str]:
    page_root = root_domain(page_url)
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().startswith(("http://", "https://")):
            continue
        rd = root_domain(href)
        if not rd or rd == page_root or rd in seen or is_skip_domain(href):
            continue
        seen.add(rd)
        out.append(href)
        if len(out) >= cap:
            break
    return out


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
# Record store with de-duplication and checkpointing
# --------------------------------------------------------------------------- #

class Store:
    def __init__(self):
        self.records: list[dict] = []
        self.seen_domains: set[str] = set()
        self.seen_names: set[str] = set()
        self.done_queries: set[str] = set()
        self.query_log: list[dict] = []
        self.per_cat: dict[str, int] = {c: 0 for c in CATEGORIES}
        self.counters = {"pages_fetched": 0, "search_requests": 0, "queries_executed": 0}
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.lock = threading.Lock()

    @property
    def total(self) -> int:
        return len(self.records)

    def count(self, category: str) -> int:
        return self.per_cat.get(category, 0)

    def add(self, rec: dict) -> bool:
        dom = norm_domain(rec["Website"])
        # Businesses without their own site (or whose "site" is a social page)
        # are keyed on their Google place id instead of the domain.
        if (not dom or is_skip_domain(dom)) and rec.get("_cid"):
            dom = f"cid:{rec['_cid']}"
        nm = norm_name(rec["Company Name"])
        with self.lock:
            if not dom or dom in self.seen_domains:
                return False
            if nm and nm in self.seen_names:
                return False
            self.seen_domains.add(dom)
            if nm:
                self.seen_names.add(nm)
            self.records.append(rec)
            self.per_cat[rec["Category"]] = self.per_cat.get(rec["Category"], 0) + 1
            return True

    def bump(self, key: str, n: int = 1):
        with self.lock:
            self.counters[key] = self.counters.get(key, 0) + n

    # -- persistence --------------------------------------------------------
    def save(self, path: str):
        with self.lock:
            payload = {
                "started_at": self.started_at,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "records": self.records,
                "seen_domains": sorted(self.seen_domains),
                "seen_names": sorted(self.seen_names),
                "done_queries": sorted(self.done_queries),
                "query_log": self.query_log,
                "counters": self.counters,
            }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Store":
        st = cls()
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        st.started_at = payload.get("started_at", st.started_at)
        st.records = payload.get("records", [])
        st.seen_domains = set(payload.get("seen_domains", []))
        st.seen_names = set(payload.get("seen_names", []))
        st.done_queries = set(payload.get("done_queries", []))
        st.query_log = payload.get("query_log", [])
        st.counters.update(payload.get("counters", {}))
        st.per_cat = {c: 0 for c in CATEGORIES}
        for r in st.records:
            st.per_cat[r.get("Category", "")] = st.per_cat.get(r.get("Category", ""), 0) + 1
        return st


# --------------------------------------------------------------------------- #
# Excel output
# --------------------------------------------------------------------------- #

def _autosize(ws):
    for col in ws.columns:
        letter = col[0].column_letter
        width = 10
        for cell in col[:500]:
            if cell.value is not None:
                width = max(width, min(60, len(str(cell.value)) + 2))
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "A2"


def _write_df(df: pd.DataFrame, path: str, sheet: str, extra: "list[tuple[str, pd.DataFrame]] | None" = None):
    tmp = path + ".tmp.xlsx"
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet[:31])
        _autosize(writer.sheets[sheet[:31]])
        for name, edf in (extra or []):
            edf.to_excel(writer, index=False, sheet_name=name[:31])
            _autosize(writer.sheets[name[:31]])
    try:
        os.replace(tmp, path)
    except PermissionError:
        logging.getLogger("output").warning("%s is open in another program; wrote %s instead", path, tmp)


def write_outputs(store: Store, out_dir: str, target: int, run_started: float, exhausted: dict):
    os.makedirs(out_dir, exist_ok=True)
    with store.lock:
        records = list(store.records)
        query_log = list(store.query_log)
        counters = dict(store.counters)
        started_at = store.started_at

    per_cat_rows = []
    for cat in CATEGORIES:
        rows = [
            {col: xlsx_safe(r.get(col, "")) for col in COLUMNS}
            for r in records if r.get("Category") == cat
        ]
        df = pd.DataFrame(rows, columns=COLUMNS)
        fname = f"{FILE_PREFIX[cat]}_{cat}.xlsx"
        _write_df(df, os.path.join(out_dir, fname), cat)
        n = len(rows)
        per_cat_rows.append({
            "Category": cat,
            "File": fname,
            "Unique Companies": n,
            "With Email": sum(1 for r in rows if r["Email"]),
            "With Phone": sum(1 for r in rows if r["Phone"]),
            "With Address": sum(1 for r in rows if r["Address"]),
            "With Services": sum(1 for r in rows if r["Services"]),
            "Queries Executed": sum(1 for q in query_log if q.get("category") == cat),
            "Queries Exhausted": "Yes" if exhausted.get(cat) else "No",
            "% of Total": round(100.0 * n / len(records), 2) if records else 0.0,
        })

    total = len(records)
    runtime_min = round((time.time() - run_started) / 60.0, 1)
    summary_rows = [
        ("Total Unique Companies", total),
        ("Target", target),
        ("Target Reached", "Yes" if total >= target else "No"),
        ("Completion %", round(100.0 * total / target, 2) if target else 100.0),
        ("Categories", len(CATEGORIES)),
        ("Records With Email", sum(1 for r in records if r.get("Email"))),
        ("Records With Phone", sum(1 for r in records if r.get("Phone"))),
        ("Records With Address", sum(1 for r in records if r.get("Address"))),
        ("Records With Services", sum(1 for r in records if r.get("Services"))),
        ("Unique Domains", len({norm_domain(r.get("Website", "")) for r in records})),
        ("Queries Executed", counters.get("queries_executed", 0)),
        ("Search Requests Sent", counters.get("search_requests", 0)),
        ("Serper Credits (this run)", counters.get("serper_credits_this_run", 0)),
        ("Pages Fetched", counters.get("pages_fetched", 0)),
        ("First Started", started_at),
        ("Last Updated", datetime.now().isoformat(timespec="seconds")),
        ("This Run Runtime (min)", runtime_min),
    ]
    summary_df = pd.DataFrame(summary_rows, columns=["Metric", "Value"])
    per_cat_df = pd.DataFrame(per_cat_rows)
    queries_df = pd.DataFrame(
        query_log, columns=["category", "query", "places", "engine_results", "candidates", "new_records", "seconds"]
    )
    _write_df(
        summary_df, os.path.join(out_dir, "00_Master_Summary.xlsx"), "Summary",
        extra=[("Per_Category", per_cat_df), ("Queries", queries_df)],
    )


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #

BATCH_TIMEOUT_SECONDS = 300  # hard cap on waiting for one query's fetch batch


def _iter_done(futures, log, label: str):
    """as_completed with a hard timeout: a wedged fetch never stalls the run.
    Unfinished futures are cancelled and abandoned."""
    try:
        yield from as_completed(futures, timeout=BATCH_TIMEOUT_SECONDS)
    except TimeoutError:
        pending = [f for f in futures if not f.done()]
        for f in pending:
            f.cancel()
        log.warning("[%s] abandoned %d stuck fetches after %ds",
                    label, len(pending), BATCH_TIMEOUT_SECONDS)


class CategoryState:
    def __init__(self, category: str, expand_locations: bool):
        self.category = category
        self.plan = self._plan(CATEGORIES[category], expand_locations)
        self.exhausted = False

    @staticmethod
    def _plan(queries: list[str], expand_locations: bool):
        for q in queries:
            yield q
        if expand_locations:
            for loc in LOCATION_MODIFIERS:
                for q in queries:
                    yield f"{q} {loc}"


class Collector:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out_dir = args.out
        os.makedirs(self.out_dir, exist_ok=True)
        self.state_path = os.path.join(self.out_dir, "collector_state.json")
        self.log = logging.getLogger("collector")
        self.target = args.target
        self.per_query = args.per_query
        self.run_started = time.time()
        self.deadline = self.run_started + args.max_minutes * 60 if args.max_minutes else None
        self.max_queries = args.max_queries
        self.queries_this_run = 0
        self.session = make_session(not args.insecure, retries=1)
        self.robots = RobotsCache(self.session)
        self.serper: Serper | None = None
        key = resolve_serper_key(args)
        if key:
            self.serper = Serper(key, not args.insecure)
            self.log.info("Serper API key found (...%s): Google search + Places enabled", key[-6:])
        else:
            self.log.warning("No Serper API key: using public engines only (slow, low yield)")
        self.search = SearchEngines(args.delay, args.engine, not args.insecure, self.serper)
        self.places_pages = args.places_pages if self.serper else 0
        self.pool = ThreadPoolExecutor(max_workers=args.workers)
        self.attempted: set[str] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        if os.path.exists(self.state_path) and not args.fresh:
            self.store = Store.load(self.state_path)
            self.log.info("Resumed checkpoint: %d records, %d queries done",
                          self.store.total, len(self.store.done_queries))
            self.attempted.update(self.store.seen_domains)
        else:
            self.store = Store()
        self.states = {c: CategoryState(c, not args.no_expand) for c in CATEGORIES}
        self._queries_since_checkpoint = 0
        self._last_checkpoint = time.time()
        self._ckpt_lock = threading.Lock()

    # -- control ------------------------------------------------------------
    def time_up(self) -> bool:
        return bool(self.deadline and time.time() >= self.deadline)

    def done(self) -> bool:
        if self.stop_event.is_set():
            return True
        if self.store.total >= self.target:
            return True
        if self.time_up():
            return True
        if self.max_queries and self.queries_this_run >= self.max_queries:
            return True
        return False

    # -- fetching -----------------------------------------------------------
    def fetch(self, url: str, max_bytes: int = 2_000_000) -> tuple[bytes | None, str]:
        try:
            with self.session.get(url, timeout=(10, 20), stream=True, allow_redirects=True) as r:
                ctype = r.headers.get("Content-Type", "").lower()
                if r.status_code >= 400:
                    return None, r.url
                if ctype and not any(t in ctype for t in ("html", "xml", "text/plain")):
                    return None, r.url
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        break
                self.store.bump("pages_fetched")
                return b"".join(chunks), r.url
        except requests.RequestException:
            return None, url
        except Exception as exc:  # pragma: no cover - defensive
            self.log.debug("fetch error %s: %s", url, exc)
            return None, url

    # -- crawling -----------------------------------------------------------
    def crawl_site(self, url: str, category: str, query: str, allow_expand: bool) -> tuple[dict | None, list[str]]:
        """Fetch one site and return (record or None, candidate URLs to crawl)."""
        if self.done():
            return None, []
        dom = norm_domain(url)
        if not dom:
            return None, []
        with self.lock:
            if dom in self.attempted:
                return None, []
            self.attempted.add(dom)

        skip = is_skip_domain(url)
        if skip and not allow_expand:
            return None, []
        if not self.robots.allowed(url):
            return None, []

        raw, final_url = self.fetch(url)
        if raw is None:
            return None, []
        try:
            soup = BeautifulSoup(raw, "html.parser")
        except Exception:
            return None, []

        candidates: list[str] = []
        if allow_expand:
            links = external_links(soup, final_url)
            if is_aggregator(final_url) or len(links) >= LIST_PAGE_MIN_EXTERNAL_LINKS:
                candidates = links

        if skip or is_skip_domain(final_url):
            return None, candidates

        page_title = clean_text(soup.title.get_text()) if soup.title else ""
        if BLOCK_TITLE_RE.search(page_title):
            self.log.debug("skipping challenge/error page %s (%s)", final_url, page_title)
            return None, candidates

        raw_text = raw.decode("utf-8", "replace")
        data = extract_company(soup, raw_text, final_url)

        if not (data.email and data.phone and data.address):
            for cp in find_contact_links(soup, final_url)[:2]:
                if self.done():
                    break
                if not self.robots.allowed(cp):
                    continue
                raw2, fu2 = self.fetch(cp)
                if raw2 is None:
                    continue
                try:
                    soup2 = BeautifulSoup(raw2, "html.parser")
                except Exception:
                    continue
                data.merge_missing(extract_company(soup2, raw2.decode("utf-8", "replace"), fu2))
                if data.email and data.phone and data.address:
                    break

        # Quality gate for crawled (non-Places) sites: a real firm's site has at
        # least one contact channel, and its name does not read like a publisher.
        if not (data.phone or data.email):
            return None, candidates
        if MEDIA_NAME_RE.search(data.name or ""):
            return None, candidates

        p = urlparse(final_url)
        website = f"{p.scheme}://{p.netloc.lower()}"
        record = {
            "Company Name": data.name or norm_domain(final_url),
            "Website": website,
            "Email": "; ".join(data.email),
            "Phone": "; ".join(data.phone),
            "Address": data.address,
            "Services": data.services,
            "Category": category,
            "Source Query": query,
            "Source URL": url,
            "Collected At": datetime.now().isoformat(timespec="seconds"),
        }
        return record, candidates

    def enrich_place(self, place: dict, category: str, query: str) -> dict | None:
        """Turn one Google Places row into a record; crawl its website (if any)
        for email / services. Name, phone and address come from Places."""
        if self.done():
            return None
        title = clean_text(place.get("title"))
        if not title:
            return None
        website = clean_text(place.get("website"))
        cid = str(place.get("cid") or place.get("placeId") or "")
        email: list[str] = []
        services = clean_text(place.get("category"))
        site_url = ""
        if website and website.lower().startswith("http"):
            dom = norm_domain(website)
            p = urlparse(website)
            site_url = f"{p.scheme}://{p.netloc.lower()}"
            with self.lock:
                already = dom in self.attempted
                self.attempted.add(dom)
            if not already and not is_skip_domain(website) and self.robots.allowed(site_url):
                raw, final_url = self.fetch(site_url)
                if raw is not None:
                    try:
                        soup = BeautifulSoup(raw, "html.parser")
                    except Exception:
                        soup = None
                    if soup is not None:
                        page_title = clean_text(soup.title.get_text()) if soup.title else ""
                        if not BLOCK_TITLE_RE.search(page_title):
                            data = extract_company(soup, raw.decode("utf-8", "replace"), final_url)
                            if not data.email:
                                for cp in find_contact_links(soup, final_url)[:1]:
                                    if not self.robots.allowed(cp):
                                        continue
                                    raw2, fu2 = self.fetch(cp)
                                    if raw2 is None:
                                        continue
                                    try:
                                        data.merge_missing(extract_company(
                                            BeautifulSoup(raw2, "html.parser"),
                                            raw2.decode("utf-8", "replace"), fu2))
                                    except Exception:
                                        pass
                            email = data.email
                            if data.services:
                                services = (services + " | " + data.services) if services else data.services
                            fp = urlparse(final_url)
                            if fp.netloc:
                                site_url = f"{fp.scheme}://{fp.netloc.lower()}"
        phone = clean_text(place.get("phoneNumber"))
        address = clean_text(place.get("address"))
        rating = place.get("rating")
        if rating is not None and place.get("ratingCount"):
            services = (services + f" | Google rating {rating} ({place.get('ratingCount')} reviews)").strip(" |")
        return {
            "Company Name": title[:150],
            "Website": site_url,
            "Email": "; ".join(email),
            "Phone": phone,
            "Address": address,
            "Services": services[:500],
            "Category": category,
            "Source Query": query,
            "Source URL": f"https://www.google.com/maps?cid={cid}" if cid else "google places",
            "Collected At": datetime.now().isoformat(timespec="seconds"),
            "_cid": cid,
        }

    def _places_many(self, places: list[dict], category: str, query: str) -> int:
        new_records = 0
        futures = [self.pool.submit(self.enrich_place, p, category, query) for p in places]
        for fut in _iter_done(futures, self.log, f"places:{query[:40]}"):
            try:
                record = fut.result()
            except Exception as exc:
                self.log.debug("places enrich error: %s", exc)
                continue
            if record and self.store.add(record):
                new_records += 1
                self.log.info("  + [%s] %s <%s> email=%s phone=%s (places)",
                              category, record["Company Name"][:50], record["Website"] or "-",
                              "yes" if record["Email"] else "no", "yes" if record["Phone"] else "no")
        return new_records

    def _crawl_many(self, urls: list[str], category: str, query: str, allow_expand: bool) -> tuple[int, list[str]]:
        new_records = 0
        extra: list[str] = []
        futures = [self.pool.submit(self.crawl_site, u, category, query, allow_expand) for u in urls]
        for fut in _iter_done(futures, self.log, f"crawl:{query[:40]}"):
            try:
                record, cands = fut.result()
            except Exception as exc:
                self.log.debug("crawl error: %s", exc)
                continue
            if record and self.store.add(record):
                new_records += 1
                self.log.info("  + [%s] %s <%s> email=%s phone=%s",
                              category, record["Company Name"][:50], record["Website"],
                              "yes" if record["Email"] else "no", "yes" if record["Phone"] else "no")
            extra.extend(cands)
        return new_records, extra

    def process_query(self, category: str, query: str) -> int:
        t0 = time.time()
        new_total = 0
        candidates_total = 0
        places_total = 0
        # 1) Google Places: structured business rows (name/phone/address/site).
        if self.places_pages and not self.done():
            places = self.search.places(query, self.places_pages)
            places_total = len(places)
            self.store.bump("search_requests")
            self.log.info("[%s] %r -> %d places", category, query, len(places))
            if places:
                new_total += self._places_many(places, category, query)
        # 2) Organic web results: crawl sites, mine directory/list pages.
        urls = self.search.search(query, self.per_query) if not self.done() else []
        self.store.bump("search_requests")
        self.log.info("[%s] %r -> %d results", category, query, len(urls))
        if urls:
            n1, extra = self._crawl_many(urls, category, query, allow_expand=True)
            new_total += n1
            if extra and not self.args.no_directory_expansion and not self.done():
                # de-dup extra candidates by domain, skip ones already attempted
                seen: set[str] = set()
                todo: list[str] = []
                for u in extra:
                    d = norm_domain(u)
                    if d and d not in seen and d not in self.attempted:
                        seen.add(d)
                        todo.append(u)
                candidates_total = len(todo)
                if todo:
                    self.log.info("[%s] mining %d linked companies from directory/list pages", category, len(todo))
                    n2, _ = self._crawl_many(todo, category, query, allow_expand=False)
                    new_total += n2
        with self.store.lock:
            self.store.done_queries.add(f"{category}||{query}")
            self.store.query_log.append({
                "category": category, "query": query, "engine_results": len(urls),
                "places": places_total, "candidates": candidates_total,
                "new_records": new_total, "seconds": round(time.time() - t0, 1),
            })
            self.store.counters["queries_executed"] = self.store.counters.get("queries_executed", 0) + 1
            if self.serper:
                self.store.counters["serper_credits_this_run"] = self.serper.credits_used
        with self.lock:
            self.queries_this_run += 1
        return new_total

    # -- public API ---------------------------------------------------------
    def search_and_collect(self, category: str, target: int) -> list[dict]:
        """Run this category's query plan until it holds `target` unique records
        (or the plan is exhausted / global stop). Returns the category's records."""
        state = self.states[category]
        while self.store.count(category) < target and not state.exhausted and not self.done():
            try:
                query = next(state.plan)
            except StopIteration:
                state.exhausted = True
                self.log.info("[%s] all queries exhausted (%d records)", category, self.store.count(category))
                break
            if f"{category}||{query}" in self.store.done_queries:
                continue
            self.process_query(category, query)
            with self.lock:
                self._queries_since_checkpoint += 1
            self.log.info("progress: %s=%d | total=%d/%d",
                          category, self.store.count(category), self.store.total, self.target)
        return [r for r in self.store.records if r["Category"] == category]

    def checkpoint(self):
        with self._ckpt_lock:
            self._queries_since_checkpoint = 0
            self._last_checkpoint = time.time()
            try:
                self.store.save(self.state_path)
                write_outputs(self.store, self.out_dir, self.target, self.run_started,
                              {c: s.exhausted for c, s in self.states.items()})
                self.log.info("checkpoint saved (%d records)", self.store.total)
            except Exception as exc:
                self.log.warning("checkpoint failed: %s", exc)

    def _run_category(self, category: str, per_round: int):
        """Worker thread: grow one category round by round, pausing when it
        gets more than two rounds ahead of the slowest live category."""
        round_no = 1
        state = self.states[category]
        try:
            while not self.done() and not state.exhausted:
                self.search_and_collect(category, per_round * round_no)
                round_no += 1
                while not self.done() and not state.exhausted:
                    live = [c for c, s in self.states.items() if not s.exhausted]
                    slowest = min(self.store.count(c) for c in live) if live else 0
                    if self.store.count(category) - slowest <= 2 * per_round:
                        break
                    time.sleep(2)
        except Exception as exc:  # keep the other categories running
            self.log.exception("[%s] worker crashed: %s", category, exc)

    def run(self):
        # Small rounds keep every category growing evenly instead of letting
        # the first category hog the whole budget.
        per_round = min(math.ceil(self.target / len(CATEGORIES)), max(1, self.args.round_size))
        threads = [
            threading.Thread(target=self._run_category, args=(cat, per_round), name=cat, daemon=True)
            for cat in CATEGORIES
        ]
        try:
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                time.sleep(1)
                due = (self._queries_since_checkpoint >= self.args.checkpoint_every
                       or time.time() - self._last_checkpoint >= self.args.checkpoint_seconds)
                if due and self._queries_since_checkpoint > 0:
                    self.checkpoint()
            if all(s.exhausted for s in self.states.values()):
                self.log.warning("Every query in every category is exhausted at %d records.", self.store.total)
        except KeyboardInterrupt:
            self.log.warning("Interrupted by user; saving progress...")
            self.stop_event.set()
        finally:
            self.stop_event.set()
            for t in threads:
                t.join(timeout=60)
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.checkpoint()
            self.report()

    def report(self):
        self.log.info("=" * 70)
        self.log.info("Total unique records: %d / %d", self.store.total, self.target)
        if self.serper:
            self.log.info("Serper credits used this run: %d%s", self.serper.credits_used,
                          f"  (disabled: {self.serper.disabled_reason})" if self.serper.disabled_reason else "")
        for cat in CATEGORIES:
            self.log.info("  %-26s %6d  %s", cat, self.store.count(cat),
                          "(exhausted)" if self.states[cat].exhausted else "")
        if self.time_up():
            self.log.info("Stopped: time budget reached. Re-run the same command to resume.")
        elif self.max_queries and self.queries_this_run >= self.max_queries:
            self.log.info("Stopped: --max-queries reached. Re-run the same command to resume.")
        self.log.info("Output folder: %s", os.path.abspath(self.out_dir))
        self.log.info("=" * 70)


# Module-level convenience wrapper so `search_and_collect(category, target)`
# can be imported and called directly.
_DEFAULT_COLLECTOR: Collector | None = None


def search_and_collect(category: str, target: int, collector: Collector | None = None) -> list[dict]:
    global _DEFAULT_COLLECTOR
    if collector is None:
        if _DEFAULT_COLLECTOR is None:
            _DEFAULT_COLLECTOR = Collector(parse_args([]))
        collector = _DEFAULT_COLLECTOR
    return collector.search_and_collect(category, target)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def resolve_serper_key(args: argparse.Namespace) -> str:
    key = (getattr(args, "serper_key", "") or "").strip()
    if not key:
        key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (os.path.join(here, "serper_key.txt"), os.path.join(os.getcwd(), "serper_key.txt")):
            if os.path.exists(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as fh:
                        key = fh.read().strip().splitlines()[0].strip() if fh else ""
                except Exception:
                    key = ""
                if key:
                    break
    return key


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Collect public business data into Excel files.")
    ap.add_argument("--target", type=int, default=15000, help="total unique records wanted (default 15000)")
    ap.add_argument("--per-query", type=int, default=10, help="organic search results crawled per query (default 10)")
    ap.add_argument("--places-pages", type=int, default=2, help="Google Places pages (10 rows each) per query via Serper (default 2)")
    ap.add_argument("--serper-key", default="", help="Serper.dev API key (or SERPER_API_KEY env var / serper_key.txt)")
    ap.add_argument("--round-size", type=int, default=100, help="records per category per round-robin pass (default 100)")
    ap.add_argument("--workers", type=int, default=32, help="parallel site fetches shared by all categories (default 32)")
    ap.add_argument("--checkpoint-seconds", type=float, default=90, help="also save state/Excel at least every N seconds (default 90)")
    ap.add_argument("--delay", type=float, default=3.0, help="base seconds between public search-engine requests (default 3)")
    ap.add_argument("--engine", choices=["auto", "serper", "ddg", "bing"], default="auto", help="search engine preference")
    ap.add_argument("--out", default="output_data", help="output directory (default ./output_data)")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after N minutes (0 = no limit)")
    ap.add_argument("--max-queries", type=int, default=0, help="stop after N queries this run (0 = no limit)")
    ap.add_argument("--checkpoint-every", type=int, default=10, help="save state/Excel every N queries (default 10)")
    ap.add_argument("--fresh", action="store_true", help="ignore existing checkpoint and start over")
    ap.add_argument("--no-expand", action="store_true", help="do not add location modifiers after base queries")
    ap.add_argument("--no-directory-expansion", action="store_true", help="do not mine company links from directory pages")
    ap.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def setup_logging(out_dir: str, verbose: bool):
    os.makedirs(out_dir, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(os.path.join(out_dir, "collector.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(out_dir: str) -> str | None:
    """Refuse to start when another collector is already writing to out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "collector.lock")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                other = int(fh.read().strip() or 0)
        except Exception:
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            print(f"Another business_collector (PID {other}) is already running in "
                  f"{os.path.abspath(out_dir)}. Exiting to avoid corrupting its output. "
                  f"Watch its progress in collector.log.", file=sys.stderr)
            return None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return path


def main(argv=None):
    args = parse_args(argv)
    lock = acquire_lock(args.out)
    if lock is None:
        sys.exit(2)
    try:
        setup_logging(args.out, args.verbose)
        log = logging.getLogger("main")
        log.info("business_collector starting (PID %d): target=%d per_query=%d workers=%d out=%s",
                 os.getpid(), args.target, args.per_query, args.workers, os.path.abspath(args.out))
        collector = Collector(args)
        collector.run()
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    main()
