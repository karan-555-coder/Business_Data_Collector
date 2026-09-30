"""Company qualification: reject news sites, blogs, directories, social pages,
rankings and other non-companies; score confidence for accepted records.
Domain lists and media patterns ported from business_collector.py."""

from __future__ import annotations

import re

from .normalize import norm_name, root_domain

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
    "ranker.com", "softwaresuggest.com", "goodreads.com", "slideshare.net",
}

MEDIA_DOMAIN_RE = re.compile(
    r"(^|[.\-])(news|magazine|mag|wiki|forum|forums|tribune|gazette|herald|"
    r"journal|times|daily|weekly|press|insider|ranking|rankings|top10|"
    r"toplist|listing|listings|directory|awards)([.\-]|$)", re.I,
)
MEDIA_NAME_RE = re.compile(
    r"\b(magazine|news|forum|wiki|wikipedia|foundation|university|college|"
    r"institute of technology|tribune|gazette|herald|journal|times|encyclopedia|"
    r"dictionary|top \d+|best \d+|\d+ best|rankings?|blog)\b", re.I,
)

# Pages on these hosts are mined for outbound company links (discovery sources)
# but never become company records themselves.
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

BLOCK_TITLE_RE = re.compile(
    r"client challenge|just a moment|attention required|access denied|"
    r"are you a human|verify you are|captcha|403 forbidden|404|not found|"
    r"page not found|error \d{3}|service unavailable|site not found|"
    r"domain (?:is )?for sale|parked domain|coming soon|under construction|"
    r"account suspended|bot detection|security check|please wait", re.I,
)

JUNK_NAME_RE = re.compile(
    r"\b(how to|what is|guide to|list of|jobs? in|vacancy|vacancies|"
    r"login|sign ?up|error|untitled)\b", re.I,
)


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


def relevance(terms: list[str], *texts: str) -> bool:
    blob = " ".join(t.lower() for t in texts if t)
    return any(term in blob for term in terms)


def validate(record: dict, from_places: bool) -> tuple[bool, str]:
    """(accepted, reason). Rejects obvious non-companies; a record must have a
    plausible name, a website or a Places listing, and at least one contact
    attribute."""
    name = record.get("Company Name", "")
    website = record.get("Official Website", "")
    if not name or len(name) < 3 or not norm_name(name):
        return False, "no plausible company name"
    if MEDIA_NAME_RE.search(name) or JUNK_NAME_RE.search(name):
        return False, f"name looks like publisher/article: {name[:40]}"
    if website and is_skip_domain(website):
        return False, f"website is a directory/social/media domain: {website}"
    if not website and not from_places:
        return False, "no official website and not a Places listing"
    if not (record.get("Business Email") or record.get("Business Phone")
            or record.get("Full Business Address") or website):
        return False, "no useful business attribute"
    return True, ""


def confidence(record: dict, terms: list[str]) -> int:
    score = 0
    if record.get("Official Website"):
        score += 25
    if record.get("Business Email"):
        score += 20
    if record.get("Business Phone"):
        score += 20
    if record.get("Full Business Address"):
        score += 15
    name = record.get("Company Name", "")
    if name and name != record.get("Official Website", "") and " " in name.strip():
        score += 10
    if relevance(terms, name, record.get("Services", ""),
                 record.get("Industry", ""), record.get("Search Query", "")):
        score += 10
    return min(score, 100)
