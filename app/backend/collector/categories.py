"""The ten fixed categories, their Excel file names, query templates and
relevance terms (used for confidence scoring, not fabrication), plus helpers
for user-defined custom categories."""

from __future__ import annotations

import re
from collections import OrderedDict

# slug -> (display name, file name, query templates, relevance terms)
CATEGORIES: "OrderedDict[str, dict]" = OrderedDict([
    ("Finance", {
        "display": "Finance",
        "file": "01_Finance.xlsx",
        "templates": [
            "financial advisory firms in {location}",
            "finance companies in {location}",
            "corporate finance firms in {location}",
        ],
        "terms": ["finance", "financial", "investment", "capital", "wealth",
                  "accounting", "audit", "tax", "advisory", "bookkeeping",
                  "payroll", "cpa", "cfo", "forensic accounting"],
        "suggested": ["Accounting", "Bookkeeping", "Tax", "Payroll", "Audit",
                      "Finance", "Financial", "CPA", "Tax Services",
                      "Corporate Tax", "CFO Services", "Forensic Accounting"],
    }),
    ("CDS_Corporate_Compliance", {
        "display": "CDS / Corporate Compliance",
        "file": "02_CDS_Corporate_Compliance.xlsx",
        "templates": [
            "company secretarial services in {location}",
            "corporate compliance firms in {location}",
            "corporate secretarial firms in {location}",
        ],
        "terms": ["secretarial", "compliance", "governance", "regulatory",
                  "company secretary", "cs firm", "corporate services"],
    }),
    ("Forms", {
        "display": "Forms / Corporate Filings",
        "file": "03_Forms.xlsx",
        "templates": [
            "business forms services in {location}",
            "corporate filing services in {location}",
            "company filing services in {location}",
        ],
        "terms": ["forms", "filing", "registration", "incorporation",
                  "documentation", "licensing"],
    }),
    ("Advisory", {
        "display": "Advisory / Consulting",
        "file": "04_Advisory.xlsx",
        "templates": [
            "business advisory firms in {location}",
            "management consulting firms in {location}",
            "strategy consulting firms in {location}",
        ],
        "terms": ["advisory", "consulting", "consultancy", "strategy",
                  "management", "transformation"],
    }),
    ("Law_Firms", {
        "display": "Law Firms",
        "file": "05_Law_Firms.xlsx",
        "templates": [
            "corporate law firms in {location}",
            "business law firms in {location}",
            "legal advisory firms in {location}",
        ],
        "terms": ["law", "legal", "attorney", "advocate", "solicitor",
                  "litigation", "counsel"],
    }),
    ("Recruitment", {
        "display": "Recruitment / Staffing",
        "file": "06_Recruitment.xlsx",
        "templates": [
            "recruitment agencies in {location}",
            "staffing companies in {location}",
            "executive search firms in {location}",
        ],
        "terms": ["recruitment", "recruiting", "staffing", "talent",
                  "executive search", "manpower", "placement", "hr"],
    }),
    ("RPO", {
        "display": "RPO",
        "file": "07_RPO.xlsx",
        "templates": [
            "RPO companies in {location}",
            "recruitment process outsourcing in {location}",
            "RPO providers in {location}",
        ],
        "terms": ["rpo", "recruitment process outsourcing", "recruitment",
                  "talent acquisition", "sourcing", "staffing", "hiring"],
    }),
    ("Medical_Healthcare", {
        "display": "Medical / Healthcare",
        "file": "08_Medical_Healthcare.xlsx",
        "templates": [
            "healthcare companies in {location}",
            "medical service providers in {location}",
            "medical companies in {location}",
        ],
        "terms": ["medical", "healthcare", "health", "clinic", "hospital",
                  "diagnostic", "pharma", "care"],
    }),
    ("3D_Studios", {
        "display": "3D Studios / 3D Visualization",
        "file": "09_3D_Studios.xlsx",
        "templates": [
            "3D studios in {location}",
            "3D visualization companies in {location}",
            "3D rendering studios in {location}",
        ],
        "terms": ["3d", "visualization", "visualisation", "rendering", "cgi",
                  "animation", "vfx", "architectural visualization", "studio"],
    }),
    ("Other_B2B", {
        "display": "Other B2B",
        "file": "10_Other_B2B.xlsx",
        "templates": [
            "B2B service providers in {location}",
            "business solutions companies in {location}",
            "industrial services firms in {location}",
        ],
        "terms": ["b2b", "business", "services", "solutions", "industrial",
                  "outsourcing", "enterprise"],
    }),
])

SUMMARY_FILE = "00_Master_Summary.xlsx"


def suggested_keywords(slug: str) -> list[str]:
    cat = CATEGORIES.get(slug)
    if not cat:
        return []
    if cat.get("suggested"):
        return cat["suggested"]
    return [t.upper() if len(t) <= 3 else t.title() for t in cat["terms"]][:8]


# ---- custom (user-defined) categories -------------------------------------- #

_GENERIC_WORDS = {"companies", "company", "firms", "firm", "agencies", "agency",
                  "services", "service", "providers", "provider", "studios",
                  "studio", "business", "businesses", "the", "of", "and", "in"}


def slugify_custom(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40]
    return "Custom_" + (s or "Category")


def custom_templates(name: str) -> list[str]:
    name = " ".join(name.split())
    core = " ".join(w for w in name.split() if w.lower() not in _GENERIC_WORDS) or name
    return [
        f"{name} in {{location}}",
        f"{core} companies in {{location}}",
        f"{core} services in {{location}}",
    ]


def custom_terms(name: str, keywords: list[str]) -> list[str]:
    """Relevance terms for a custom category: its meaningful words plus the
    user's keywords."""
    terms = [name.lower()]
    for src in [name] + list(keywords):
        for w in re.split(r"[^A-Za-z0-9+#]+", src.lower()):
            if len(w) >= 2 and w not in _GENERIC_WORDS and w not in terms:
                terms.append(w)
    return terms


# Alternative search phrases per category: distinct SUB-NICHES (service +
# business-type), not rephrasings. Different sub-niches surface different
# companies; "top/best/leading X" returns the same ones (measured 0-1 new
# businesses per credit), so modifiers are no longer used for expansion.
CATEGORY_PHRASES: dict[str, list[str]] = {
    "Finance": ["accounting firm", "CPA firm", "bookkeeping services",
                "tax preparation services", "payroll services company",
                "wealth management firm", "investment advisory firm",
                "fractional CFO services", "forensic accountant", "audit firm",
                "business valuation firm", "financial planner"],
    "CDS_Corporate_Compliance": [
        "company secretarial services", "registered agent services",
        "entity management services", "corporate governance advisory",
        "regulatory compliance consulting", "business incorporation services",
        "annual compliance filing services", "statutory compliance services",
        "compliance outsourcing company"],
    "Forms": ["business formation services", "company registration services",
              "LLC formation service", "registered agent",
              "business license services", "trademark filing services",
              "document preparation services", "notary public services",
              "corporate filing agent", "incorporation services"],
    "Advisory": ["management consulting firm", "business consultant",
                 "strategy consulting firm", "operations consulting",
                 "small business consultant", "change management consultants",
                 "restructuring advisory firm", "M&A advisory firm",
                 "process improvement consultants", "HR consulting firm",
                 "business advisory services"],
    "Law_Firms": ["corporate lawyer", "business attorney", "commercial law firm",
                  "contract lawyer", "employment law firm",
                  "intellectual property attorney", "mergers and acquisitions lawyer",
                  "tax attorney", "commercial litigation attorney", "startup lawyer"],
    "Recruitment": ["recruitment agency", "staffing agency", "executive search firm",
                    "IT staffing company", "temp agency", "headhunter",
                    "healthcare staffing agency", "engineering recruitment firm",
                    "finance recruitment agency", "light industrial staffing"],
    "RPO": ["recruitment process outsourcing", "RPO provider",
            "talent acquisition outsourcing", "outsourced recruiting services",
            "on-demand recruiting", "recruiting as a service",
            "offshore recruitment services", "hiring outsourcing company"],
    "Medical_Healthcare": ["medical clinic", "diagnostic center", "urgent care center",
                           "physical therapy clinic", "medical laboratory",
                           "home health care agency", "medical billing company",
                           "outpatient surgery center", "imaging center",
                           "dental clinic"],
    "3D_Studios": ["architectural visualization studio", "3D rendering services",
                   "3D animation studio", "CGI studio", "product rendering company",
                   "3D modeling services", "interior rendering services",
                   "VFX studio", "virtual tour company", "motion graphics studio",
                   "architectural animation company"],
    "Other_B2B": ["business process outsourcing company", "IT services company",
                  "facility management company", "commercial cleaning company",
                  "logistics services company", "managed IT services",
                  "industrial supply company", "B2B marketing agency",
                  "commercial printing company", "office equipment supplier"],
}


# Held back for the planner's "expanded" tier (used only once the phrases
# above are exhausted): genuine synonyms and narrower sub-niches of the same
# business type - different wording that surfaces different real companies.
CATEGORY_EXPANSION: dict[str, list[str]] = {
    "Finance": ["chartered accountant", "tax consultant", "GST consultant",
                "accounts outsourcing company", "virtual CFO", "internal audit firm",
                "financial consultant", "investment banking boutique",
                "small business accountant", "accounting and tax advisory"],
    "CDS_Corporate_Compliance": [
        "company secretary firm", "ROC filing services", "corporate law compliance",
        "secretarial audit firm", "board governance consultants",
        "compliance management services", "business compliance consultants",
        "corporate secretarial outsourcing", "KYC compliance services"],
    "Forms": ["company formation agent", "business registration consultant",
              "GST registration services", "trademark registration agent",
              "import export code registration", "MSME registration services",
              "business permit services", "legal document services",
              "incorporation consultant"],
    "Advisory": ["business strategy consultants", "growth consulting firm",
                 "management advisory services", "operations excellence consultants",
                 "digital transformation consulting", "business turnaround consultant",
                 "risk advisory firm", "family business consultants",
                 "market entry consultants", "lean six sigma consultants"],
    "Law_Firms": ["corporate advocate", "business litigation lawyer",
                  "commercial dispute lawyer", "legal consultants for companies",
                  "banking and finance lawyer", "real estate law firm",
                  "arbitration lawyer", "compliance lawyer", "patent attorney",
                  "labour law consultant"],
    "Recruitment": ["talent acquisition firm", "technical staffing agency",
                    "IT recruitment company", "contract staffing services",
                    "permanent placement agency", "manpower consultancy",
                    "HR outsourcing company", "sales recruitment agency",
                    "hospitality staffing agency", "warehouse staffing agency",
                    "construction staffing agency", "nurse staffing agency",
                    "legal recruitment agency", "campus recruitment services"],
    "RPO": ["recruitment outsourcing company", "talent sourcing services",
            "end to end recruitment outsourcing", "project RPO services",
            "embedded recruiter services", "recruitment support services",
            "hiring process outsourcing", "sourcing and screening services"],
    "Medical_Healthcare": ["multispecialty clinic", "pathology lab", "polyclinic",
                           "radiology center", "physiotherapy center",
                           "healthcare staffing company", "medical equipment supplier",
                           "eye clinic", "dialysis center", "nursing home"],
    "3D_Studios": ["3D walkthrough company", "architectural rendering company",
                   "3D product animation", "3D visualization agency",
                   "real estate 3D rendering", "BIM modeling services",
                   "3D printing prototyping studio", "game art studio",
                   "AR VR development studio", "explainer video animation studio"],
    "Other_B2B": ["IT consulting company", "software development company",
                  "digital marketing agency", "corporate event management company",
                  "security services company", "staff transport services",
                  "industrial automation company", "packaging solutions company",
                  "corporate gifting company", "call center outsourcing"],
}
_EXPANSION_SUFFIXES = ["consultants", "contractors", "services company"]


def expansion_phrases(slug: str, display: str, keywords: list[str]) -> list[str]:
    """Extra phrases for the "expanded" tier: the category's curated
    synonyms/sub-niches, plus the user's keywords and custom-category name
    combined with alternative business descriptions. The planner drops any
    that are trivial rephrasings of phrases already used."""
    out: list[str] = []
    seen: set[str] = set()

    def push(p: str):
        p = " ".join(p.split())
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)

    for p in CATEGORY_EXPANSION.get(slug, []):
        push(p)
    bases = list(keywords)
    if slug not in CATEGORIES:
        core = " ".join(w for w in display.split()
                        if w.lower() not in _GENERIC_WORDS) or display
        bases.append(core)
    for b in bases:
        for suffix in _EXPANSION_SUFFIXES:
            push(f"{b} {suffix}")
    return out


def search_phrases(slug: str, display: str, keywords: list[str]) -> list[str]:
    """Ordered phrase list for the discovery planner: the user's keywords
    first, then the category's template phrases, then its sub-niche phrases.
    (Trivial rephrasings are dropped later by the planner's canonical key.)"""
    out: list[str] = []
    seen: set[str] = set()

    def push(p: str):
        p = " ".join(p.split())
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)

    for kw in keywords:
        push(kw)
    if slug in CATEGORIES:
        for tpl in CATEGORIES[slug]["templates"]:
            push(tpl.replace(" in {location}", ""))
        for p in CATEGORY_PHRASES.get(slug, []):
            push(p)
    else:
        for tpl in custom_templates(display):
            push(tpl.replace(" in {location}", ""))
    return out


# Query expansion: used to keep searching when the base queries run out
# before the target is reached.
QUERY_MODIFIERS = ["top", "best", "leading", "trusted", "boutique",
                   "independent", "professional", "specialist"]

MAJOR_CITIES = {
    "india": ["Mumbai", "Delhi", "Bangalore", "Hyderabad", "Chennai", "Pune",
              "Kolkata", "Ahmedabad", "Gurgaon", "Noida", "Jaipur", "Surat",
              "Lucknow", "Indore", "Kochi", "Coimbatore", "Nagpur",
              "Chandigarh", "Bhopal", "Visakhapatnam"],
    "usa": ["New York", "Los Angeles", "Chicago", "Houston", "Dallas",
            "Atlanta", "Boston", "San Francisco", "Seattle", "Miami",
            "Denver", "Phoenix", "Austin", "Charlotte", "San Diego",
            "San Jose", "Philadelphia", "Columbus", "Indianapolis",
            "Nashville", "Portland", "Las Vegas", "Detroit", "Minneapolis",
            "Tampa", "Orlando", "Baltimore", "St. Louis", "Kansas City",
            "Pittsburgh", "Cincinnati", "Raleigh", "Salt Lake City"],
    "uk": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow",
           "Edinburgh", "Bristol", "Liverpool", "Sheffield", "Newcastle",
           "Nottingham", "Cardiff", "Belfast", "Leicester"],
    "uae": ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah"],
    "canada": ["Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa",
               "Edmonton", "Winnipeg", "Quebec City", "Hamilton"],
    "australia": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide",
                  "Gold Coast", "Canberra", "Newcastle"],
    "germany": ["Berlin", "Munich", "Frankfurt", "Hamburg", "Cologne",
                "Stuttgart", "Dusseldorf", "Leipzig"],
    "france": ["Paris", "Lyon", "Marseille", "Toulouse", "Nice", "Bordeaux"],
    "netherlands": ["Amsterdam", "Rotterdam", "The Hague", "Utrecht",
                    "Eindhoven"],
    "ireland": ["Dublin", "Cork", "Galway", "Limerick"],
    "south africa": ["Johannesburg", "Cape Town", "Durban", "Pretoria"],
    "philippines": ["Manila", "Cebu", "Davao", "Quezon City"],
    "malaysia": ["Kuala Lumpur", "Penang", "Johor Bahru"],
    "new zealand": ["Auckland", "Wellington", "Christchurch"],
    "saudi arabia": ["Riyadh", "Jeddah", "Dammam"],
    "singapore": ["Singapore"],
}
_COUNTRY_ALIASES = {
    "united states": "usa", "united states of america": "usa", "us": "usa",
    "america": "usa", "u.s.": "usa", "u.s.a.": "usa",
    "united kingdom": "uk", "great britain": "uk", "england": "uk",
    "united arab emirates": "uae",
}


def cities_for(country: str) -> list[str]:
    key = " ".join((country or "").lower().split())
    return MAJOR_CITIES.get(_COUNTRY_ALIASES.get(key, key), [])


def build_query_pool(templates: list[str], keywords: list[str], location: str,
                     country: str, expand_cities: bool,
                     max_queries: int) -> list[str]:
    """Base queries first, then automatic variations (city expansion within
    the chosen country, then modifier variants) so the collector can keep
    searching until the target is reached. Capped at max_queries."""
    out = build_queries(templates, keywords, location, max_queries)
    seen = {q.lower() for q in out}

    def push(q: str):
        q = " ".join(q.split())
        if q and q.lower() not in seen and len(out) < max_queries:
            seen.add(q.lower())
            out.append(q)

    cities = cities_for(country) if expand_cities else []
    kws = [k.strip() for k in keywords if k.strip()]

    # Tier 2: breadth-first over cities - one phrasing across EVERY city
    # before a second phrasing anywhere. Different phrasings of the same city
    # return largely the same firms (measured: 72% repeat hits when cities
    # were searched depth-first), while a new city returns new firms.
    for kw in kws:
        for city in cities:
            push(f"{kw} in {city}")
    for tpl in templates:
        for city in cities:
            push(tpl.format(location=city))
    # Tier 3: modifier variants at the base location.
    for mod in QUERY_MODIFIERS:
        for tpl in templates:
            q = tpl.format(location=location) if location \
                else tpl.replace(" in {location}", "")
            push(f"{mod} {q}")
        for kw in kws:
            push(f"{mod} {kw} in {location}" if location else f"{mod} {kw}")
    # Tier 4: modifier x city - hundreds of further unique variations, so a
    # large target never runs out of searches prematurely.
    for mod in QUERY_MODIFIERS:
        for kw in kws:
            for city in cities:
                push(f"{mod} {kw} in {city}")
        for tpl in templates:
            for city in cities:
                push(f"{mod} {tpl.format(location=city)}")
    return out[:max_queries]


def build_queries(templates: list[str], keywords: list[str], location: str,
                  max_queries: int) -> list[str]:
    """Controlled query list: user keywords first, then category templates.
    Duplicates removed, capped at max_queries."""
    location = location.strip()
    out: list[str] = []
    seen: set[str] = set()

    def push(q: str):
        q = " ".join(q.split())
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)

    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        push(f"{kw} in {location}" if location else kw)
        push(f"{kw} companies in {location}" if location else f"{kw} companies")
    for tpl in templates:
        push(tpl.format(location=location) if location else
             tpl.replace(" in {location}", ""))
    return out[:max_queries]
