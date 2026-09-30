"""Geographic expansion data for the discovery planner.

A new city returns genuinely new local businesses; a rephrased query in the
same place mostly returns the same ones (measured: 0-1 new businesses per
credit for "top/best X in <state>" rephrasings). So when the user pins a
state, the planner spreads searches over that state's cities, and for a
country-wide search over cities across its states."""

from __future__ import annotations

STATE_CITIES: dict[str, dict[str, list[str]]] = {
    "usa": {
        "Alabama": ["Birmingham", "Huntsville", "Montgomery", "Mobile", "Tuscaloosa"],
        "Alaska": ["Anchorage", "Fairbanks", "Juneau"],
        "Arizona": ["Phoenix", "Tucson", "Scottsdale", "Mesa", "Chandler", "Tempe"],
        "Arkansas": ["Little Rock", "Fayetteville", "Fort Smith", "Bentonville"],
        "California": ["Los Angeles", "San Francisco", "San Diego", "San Jose",
                       "Sacramento", "Oakland", "Irvine", "Fresno", "Long Beach",
                       "Santa Monica", "Pasadena", "Palo Alto"],
        "Colorado": ["Denver", "Colorado Springs", "Boulder", "Fort Collins", "Aurora"],
        "Connecticut": ["Hartford", "Stamford", "New Haven", "Bridgeport", "Greenwich"],
        "Delaware": ["Wilmington", "Dover", "Newark"],
        "Florida": ["Miami", "Orlando", "Tampa", "Jacksonville", "Fort Lauderdale",
                    "St. Petersburg", "Boca Raton", "West Palm Beach", "Tallahassee",
                    "Sarasota", "Naples"],
        "Georgia": ["Atlanta", "Savannah", "Augusta", "Alpharetta", "Marietta", "Athens"],
        "Hawaii": ["Honolulu", "Hilo", "Kailua"],
        "Idaho": ["Boise", "Meridian", "Idaho Falls", "Coeur d'Alene"],
        "Illinois": ["Chicago", "Naperville", "Schaumburg", "Springfield", "Peoria",
                     "Rockford", "Evanston"],
        "Indiana": ["Indianapolis", "Fort Wayne", "Carmel", "Evansville", "South Bend"],
        "Iowa": ["Des Moines", "Cedar Rapids", "Iowa City", "Davenport"],
        "Kansas": ["Wichita", "Overland Park", "Kansas City", "Topeka"],
        "Kentucky": ["Louisville", "Lexington", "Bowling Green"],
        "Louisiana": ["New Orleans", "Baton Rouge", "Shreveport", "Lafayette"],
        "Maine": ["Portland", "Bangor", "Augusta"],
        "Maryland": ["Baltimore", "Bethesda", "Rockville", "Columbia", "Annapolis",
                     "Silver Spring"],
        "Massachusetts": ["Boston", "Cambridge", "Worcester", "Springfield", "Newton",
                          "Waltham"],
        "Michigan": ["Detroit", "Grand Rapids", "Ann Arbor", "Troy", "Lansing",
                     "Southfield"],
        "Minnesota": ["Minneapolis", "St. Paul", "Bloomington", "Rochester", "Duluth"],
        "Mississippi": ["Jackson", "Gulfport", "Hattiesburg"],
        "Missouri": ["St. Louis", "Kansas City", "Springfield", "Columbia"],
        "Montana": ["Billings", "Missoula", "Bozeman"],
        "Nebraska": ["Omaha", "Lincoln"],
        "Nevada": ["Las Vegas", "Reno", "Henderson"],
        "New Hampshire": ["Manchester", "Nashua", "Portsmouth", "Concord"],
        "New Jersey": ["Newark", "Jersey City", "Princeton", "Hoboken", "Morristown",
                       "Edison", "Trenton"],
        "New Mexico": ["Albuquerque", "Santa Fe", "Las Cruces"],
        "New York": ["New York City", "Brooklyn", "Buffalo", "Rochester", "Albany",
                     "Long Island", "White Plains", "Syracuse"],
        "North Carolina": ["Charlotte", "Raleigh", "Durham", "Greensboro",
                           "Winston-Salem", "Asheville", "Cary"],
        "North Dakota": ["Fargo", "Bismarck"],
        "Ohio": ["Columbus", "Cleveland", "Cincinnati", "Dayton", "Toledo", "Akron"],
        "Oklahoma": ["Oklahoma City", "Tulsa", "Norman"],
        "Oregon": ["Portland", "Eugene", "Salem", "Bend", "Beaverton"],
        "Pennsylvania": ["Philadelphia", "Pittsburgh", "Harrisburg", "Allentown",
                         "King of Prussia", "Lancaster"],
        "Rhode Island": ["Providence", "Warwick", "Newport"],
        "South Carolina": ["Charleston", "Columbia", "Greenville", "Myrtle Beach"],
        "South Dakota": ["Sioux Falls", "Rapid City"],
        "Tennessee": ["Nashville", "Memphis", "Knoxville", "Chattanooga", "Franklin"],
        "Texas": ["Houston", "Dallas", "Austin", "San Antonio", "Fort Worth",
                  "Plano", "El Paso", "Irving", "Frisco", "Arlington", "The Woodlands",
                  "Corpus Christi", "Lubbock", "Sugar Land", "McKinney"],
        "Utah": ["Salt Lake City", "Provo", "Ogden", "Lehi", "St. George"],
        "Vermont": ["Burlington", "Montpelier"],
        "Virginia": ["Richmond", "Virginia Beach", "Arlington", "Alexandria",
                     "Norfolk", "Reston", "McLean"],
        "Washington": ["Seattle", "Bellevue", "Tacoma", "Spokane", "Redmond",
                       "Vancouver"],
        "West Virginia": ["Charleston", "Morgantown", "Huntington"],
        "Wisconsin": ["Milwaukee", "Madison", "Green Bay", "Waukesha"],
        "Wyoming": ["Cheyenne", "Casper", "Jackson"],
        "District of Columbia": ["Washington DC"],
    },
    "india": {
        "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Thane", "Navi Mumbai", "Nashik"],
        "Karnataka": ["Bangalore", "Mysore", "Mangalore", "Hubli"],
        "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli"],
        "Telangana": ["Hyderabad", "Secunderabad", "Warangal"],
        "Delhi": ["New Delhi", "Delhi"],
        "Haryana": ["Gurgaon", "Faridabad", "Panchkula"],
        "Uttar Pradesh": ["Noida", "Lucknow", "Ghaziabad", "Kanpur", "Varanasi"],
        "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot", "Gandhinagar"],
        "West Bengal": ["Kolkata", "Howrah", "Siliguri"],
        "Rajasthan": ["Jaipur", "Udaipur", "Jodhpur"],
        "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode"],
        "Madhya Pradesh": ["Indore", "Bhopal", "Gwalior"],
        "Punjab": ["Chandigarh", "Ludhiana", "Amritsar", "Mohali"],
        "Andhra Pradesh": ["Visakhapatnam", "Vijayawada", "Guntur"],
        "Odisha": ["Bhubaneswar", "Cuttack"],
    },
    "canada": {
        "Ontario": ["Toronto", "Ottawa", "Mississauga", "Hamilton", "London",
                    "Markham", "Kitchener"],
        "Quebec": ["Montreal", "Quebec City", "Laval", "Gatineau"],
        "British Columbia": ["Vancouver", "Victoria", "Surrey", "Burnaby", "Kelowna"],
        "Alberta": ["Calgary", "Edmonton", "Red Deer"],
        "Manitoba": ["Winnipeg", "Brandon"],
        "Saskatchewan": ["Saskatoon", "Regina"],
        "Nova Scotia": ["Halifax"],
        "New Brunswick": ["Moncton", "Saint John", "Fredericton"],
    },
    "australia": {
        "New South Wales": ["Sydney", "Newcastle", "Wollongong", "Parramatta"],
        "Victoria": ["Melbourne", "Geelong", "Ballarat"],
        "Queensland": ["Brisbane", "Gold Coast", "Sunshine Coast", "Townsville", "Cairns"],
        "Western Australia": ["Perth", "Fremantle"],
        "South Australia": ["Adelaide"],
        "Tasmania": ["Hobart", "Launceston"],
        "Australian Capital Territory": ["Canberra"],
    },
    "uk": {
        "England": ["London", "Manchester", "Birmingham", "Leeds", "Bristol",
                    "Liverpool", "Sheffield", "Newcastle upon Tyne", "Nottingham",
                    "Leicester", "Reading", "Cambridge", "Oxford", "Milton Keynes",
                    "Southampton", "Brighton"],
        "Scotland": ["Glasgow", "Edinburgh", "Aberdeen", "Dundee"],
        "Wales": ["Cardiff", "Swansea", "Newport"],
        "Northern Ireland": ["Belfast", "Derry"],
    },
    "uae": {
        "Dubai": ["Dubai"], "Abu Dhabi": ["Abu Dhabi", "Al Ain"],
        "Sharjah": ["Sharjah"], "Ajman": ["Ajman"],
        "Ras Al Khaimah": ["Ras Al Khaimah"],
    },
}

US_STATE_ABBR = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas",
    "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota",
    "ms": "Mississippi", "mo": "Missouri", "mt": "Montana", "ne": "Nebraska",
    "nv": "Nevada", "nh": "New Hampshire", "nj": "New Jersey",
    "nm": "New Mexico", "ny": "New York", "nc": "North Carolina",
    "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma", "or": "Oregon",
    "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "vt": "Vermont", "va": "Virginia", "wa": "Washington",
    "wv": "West Virginia", "wi": "Wisconsin", "wy": "Wyoming",
    "dc": "District of Columbia",
}


def _country_key(country: str) -> str:
    from .categories import _COUNTRY_ALIASES
    key = " ".join((country or "").lower().split())
    return _COUNTRY_ALIASES.get(key, key)


def find_state(state: str, country: str) -> tuple[str, str, list[str]]:
    """Resolve a user-typed state (full name or US abbreviation) to
    (country key, canonical state name, its cities). Unknown -> ("", "", [])."""
    s = " ".join((state or "").lower().replace(".", "").split())
    if not s:
        return "", "", []
    ckey = _country_key(country)
    if ckey in ("usa", "") and s in US_STATE_ABBR:
        name = US_STATE_ABBR[s]
        return "usa", name, STATE_CITIES["usa"][name]
    order = [ckey] + [k for k in STATE_CITIES if k != ckey] if ckey in STATE_CITIES \
        else list(STATE_CITIES)
    for ck in order:
        for name, cities in STATE_CITIES[ck].items():
            if name.lower() == s:
                return ck, name, cities
    return "", "", []


def nearby_geos(city: str, state: str, country: str) -> list[str]:
    """Extra locations for the planner's "expanded" tier (used only after the
    base locations are exhausted). A pinned city widens to the other cities
    of its state plus the state itself; a pinned state or a country-wide
    search already covers its cities, so nothing is added (the search never
    leaves the region the user chose)."""
    if not city:
        return []
    ckey = _country_key(country)
    sname, cities = "", []
    if state:
        _, sname, cities = find_state(state, country)
    if not cities:   # state not given: find the state that lists this city
        order = [ckey] if ckey in STATE_CITIES else list(STATE_CITIES)
        for ck in order:
            for name, cs in STATE_CITIES[ck].items():
                if any(c.lower() == city.lower() for c in cs):
                    sname, cities = name, cs
                    break
            if cities:
                break
    if not cities:
        return []
    out = [f"{c}, {sname}" for c in cities if c.lower() != city.lower()]
    out.append(f"{sname}, {country}" if country else sname)
    return out


def expansion_geos(city: str, state: str, country: str, location: str,
                   major_cities: list[str]) -> list[str]:
    """Ordered search locations. The user's own location always comes first.
    - city pinned: that city only (diversity then comes from phrases)
    - state pinned: its cities as "City, State"
    - country only: major cities, then round-robin over every state's cities
      (breadth first, so each new search lands in a new market)."""
    out: list[str] = []
    seen: set[str] = set()

    def push(g: str):
        g = " ".join(g.split())
        if g and g.lower() not in seen:
            seen.add(g.lower())
            out.append(g)

    push(location)
    if city:
        return out
    if state:
        _, sname, cities = find_state(state, country)
        for c in cities:
            push(f"{c}, {sname}")
        return out
    for c in major_cities:
        push(c)
    majors = {c.lower() for c in major_cities}
    states = STATE_CITIES.get(_country_key(country), {})
    depth = max((len(v) for v in states.values()), default=0)
    for i in range(depth):
        for sname, cities in states.items():
            if i < len(cities) and cities[i].lower() not in majors:
                push(f"{cities[i]}, {sname}")
    return out
