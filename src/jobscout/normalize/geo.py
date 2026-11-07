"""Country and remote-region detection from free-text locations.

Deliberately a lookup table, not a geocoder: crawls must be cheap, deterministic and offline.
Unknown places return nothing rather than a guess.
"""

from __future__ import annotations

import re

# alias (lowercase) -> ISO-3166 alpha-2
_COUNTRY_ALIASES: dict[str, str] = {}


def _add(code: str, *aliases: str) -> None:
    for alias in aliases:
        _COUNTRY_ALIASES[alias.lower()] = code


_add(
    "KZ",
    "kazakhstan",
    "казахстан",
    "қазақстан",
    "almaty",
    "алматы",
    "алма-ата",
    "astana",
    "астана",
    "nur-sultan",
    "нур-султан",
    "shymkent",
    "шымкент",
    "karaganda",
    "караганда",
    "aktobe",
    "актобе",
    "atyrau",
    "атырау",
    "aktau",
    "актау",
    "pavlodar",
    "павлодар",
    "ust-kamenogorsk",
    "усть-каменогорск",
    "oskemen",
    "kostanay",
    "костанай",
    "taraz",
    "тараз",
)
_add(
    "US",
    "united states",
    "usa",
    "u.s.",
    "us",
    "san francisco",
    "new york",
    "nyc",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "los angeles",
    "palo alto",
    "mountain view",
    "sunnyvale",
    "menlo park",
    "denver",
    "atlanta",
    "washington, dc",
    "bay area",
    "california",
    "ca, us",
)
_add("GB", "united kingdom", "uk", "england", "london", "manchester", "edinburgh", "cambridge, uk")
_add("DE", "germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt")
_add("NL", "netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven")
_add("PL", "poland", "polska", "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "wrocław")
_add("JP", "japan", "tokyo", "osaka", "kyoto", "fukuoka", "yokohama", "япония", "токио")
_add("AE", "united arab emirates", "uae", "dubai", "abu dhabi", "дубай", "оаэ")
_add("SA", "saudi arabia", "ksa", "riyadh", "jeddah", "dammam")
_add("QA", "qatar", "doha")
_add("SG", "singapore", "сингапур")
_add("IE", "ireland", "dublin")
_add("FR", "france", "paris")
_add("ES", "spain", "madrid", "barcelona")
_add("PT", "portugal", "lisbon", "lisboa", "porto")
_add("CH", "switzerland", "zurich", "zürich", "geneva", "lausanne")
_add("SE", "sweden", "stockholm")
_add("FI", "finland", "helsinki")
_add("DK", "denmark", "copenhagen")
_add("EE", "estonia", "tallinn")
_add("LT", "lithuania", "vilnius")
_add("CZ", "czech republic", "czechia", "prague")
_add("RS", "serbia", "belgrade")
_add("GE", "georgia (country)", "tbilisi", "тбилиси")
_add("AM", "armenia", "yerevan", "ереван")
_add("UZ", "uzbekistan", "tashkent", "ташкент", "узбекистан")
_add("KG", "kyrgyzstan", "bishkek", "бишкек", "кыргызстан")
_add("AZ", "azerbaijan", "baku", "баку")
_add("TR", "turkey", "türkiye", "istanbul", "стамбул")
_add("CY", "cyprus", "limassol", "nicosia", "кипр")
_add("RU", "russia", "россия", "moscow", "москва", "saint petersburg", "санкт-петербург")
_add("CA", "canada", "toronto", "vancouver", "montreal", "montréal")
_add("IN", "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "gurgaon")
_add("CN", "china", "shanghai", "beijing", "shenzhen")
_add("HK", "hong kong")
_add("KR", "south korea", "korea", "seoul")
_add("TW", "taiwan", "taipei")
_add("AU", "australia", "sydney", "melbourne")
_add("IL", "israel", "tel aviv")
_add("BR", "brazil", "são paulo", "sao paulo")
_add("MX", "mexico", "mexico city")
_add("AR", "argentina", "buenos aires")

# Longest alias first so "new york" wins over "york"; word boundaries keep "us" out of "focus".
_ALIAS_RE = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(a) for a in sorted(_COUNTRY_ALIASES, key=len, reverse=True))
    + r")(?!\w)"
)

EU = {
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
}

_REGION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "WORLD",
        re.compile(
            r"\b(worldwide|anywhere|global(ly)?|any location|fully remote, any)\b"
            r"|по всему миру|из любой (точки|страны)",
            re.I,
        ),
    ),
    ("EU", re.compile(r"\b(europe|european union|eu)\b|европ", re.I)),
    ("EMEA", re.compile(r"\bemea\b", re.I)),
    ("AMERICAS", re.compile(r"\b(americas|north america|latam|latin america)\b", re.I)),
    ("APAC", re.compile(r"\b(apac|asia[- ]pacific)\b", re.I)),
    ("CIS", re.compile(r"\b(cis)\b|\bснг\b", re.I)),
]

_SPLIT = re.compile(r"[;/|•·]|\s+-\s+|\s+or\s+|\(|\)")


def detect_countries(text: str | None) -> list[str]:
    """Return ISO2 codes mentioned in a location string, in order of appearance."""
    if not text:
        return []
    seen: list[str] = []
    for match in _ALIAS_RE.finditer(text.lower()):
        code = _COUNTRY_ALIASES[match.group(0)]
        if code not in seen:
            seen.append(code)
    return seen


def detect_regions(text: str | None) -> list[str]:
    if not text:
        return []
    return [token for token, pattern in _REGION_PATTERNS if pattern.search(text)]


_EMEA_EXTRA = {"GB", "CH", "NO", "AE", "SA", "QA", "IL", "TR", "RS", "UA", "GE", "AM", "KZ", "EG"}
_CIS = {"KZ", "UZ", "KG", "AM", "AZ", "GE", "RU", "BY", "TJ"}
_AMERICAS = {"US", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "UY"}
_APAC = {"JP", "SG", "KR", "TW", "HK", "CN", "AU", "NZ", "IN", "ID", "MY", "TH", "VN", "PH"}


def regions_open_to(country: str) -> list[str]:
    """Every remote-region token that admits a candidate living in `country`."""
    country = country.upper()
    tokens = ["WORLD", country]
    if country in EU:
        tokens.append("EU")
    if country in EU or country in _EMEA_EXTRA:
        tokens.append("EMEA")
    if country in _CIS:
        tokens.append("CIS")
    if country in _AMERICAS:
        tokens.append("AMERICAS")
    if country in _APAC:
        tokens.append("APAC")
    return tokens


def country_in_regions(country: str, regions: list[str]) -> bool:
    """True if a candidate living in `country` may be hired into a remote role open to `regions`.

    An empty list means "not restricted" (nothing stated).
    """
    return not regions or bool(set(regions) & set(regions_open_to(country)))
