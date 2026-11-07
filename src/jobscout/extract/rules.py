"""Deterministic baseline extractor.

Runs offline in microseconds, is the fallback when no LLM key is configured, and is the
baseline the LLM extractor has to beat on the labelled eval set (see jobscout.evals).
"""

from __future__ import annotations

import re
from typing import Any

from jobscout.domain import JobFacts, RawPosting, RemotePolicy, Salary, Seniority
from jobscout.normalize.geo import detect_countries, detect_regions
from jobscout.normalize.salary import canonical_currency

# --- seniority ---------------------------------------------------------------------------

_TITLE_LEVELS: list[tuple[Seniority, re.Pattern[str]]] = [
    (
        Seniority.INTERN,
        re.compile(r"\b(intern|internship|trainee|стажер|стажёр|стажировка)\b", re.I),
    ),
    (Seniority.PRINCIPAL, re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    (
        Seniority.LEAD,
        re.compile(
            r"\b(lead|staff|head of|team ?lead|tech ?lead|тимлид|техлид|руководитель|ведущий)\b",
            re.I,
        ),
    ),
    (Seniority.SENIOR, re.compile(r"\b(senior|sr\.?|старший|сеньор|синьор)\b", re.I)),
    (Seniority.MID, re.compile(r"\b(middle|mid|mid-level|intermediate|мидл)\b|\bii\b", re.I)),
    (
        Seniority.JUNIOR,
        re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|младший|джун\w*)\b", re.I),
    ),
]
_HINT_LEVELS = {
    "intern": Seniority.INTERN,
    "internship": Seniority.INTERN,
    "entry": Seniority.JUNIOR,
    "entry level": Seniority.JUNIOR,
    "junior": Seniority.JUNIOR,
    "associate": Seniority.JUNIOR,
    "mid": Seniority.MID,
    "mid-level": Seniority.MID,
    "mid-senior level": Seniority.SENIOR,
    "senior": Seniority.SENIOR,
    "lead": Seniority.LEAD,
    "staff": Seniority.LEAD,
    "manager": Seniority.LEAD,
    "director": Seniority.PRINCIPAL,
    "executive": Seniority.PRINCIPAL,
    "principal": Seniority.PRINCIPAL,
}


def seniority_from_title(title: str) -> Seniority:
    for level, pattern in _TITLE_LEVELS:
        if pattern.search(title):
            return level
    return Seniority.UNKNOWN


def seniority_from_years(years: int | None) -> Seniority:
    if years is None:
        return Seniority.UNKNOWN
    if years <= 1:
        return Seniority.JUNIOR
    if years <= 4:
        return Seniority.MID
    return Seniority.SENIOR


# --- years of experience -----------------------------------------------------------------

_YEARS = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"at least (\d{1,2}) (?:years|yrs)", re.I),
    re.compile(
        r"(?:опыт\w*[^.\n]{0,40}?)(?:от|не менее|более)\s*(\d{1,2})(?:-?х)?\s*(?:лет|года|год)",
        re.I,
    ),
    re.compile(r"(\d{1,2})\+?\s*(?:лет|года|год)\s+(?:опыт|коммерческ)", re.I),
]


def years_min(text: str) -> int | None:
    values = [int(m) for rx in _YEARS for m in rx.findall(text)]
    values = [v for v in values if 0 < v <= 15]
    # Take the strongest requirement: "3+ years Python, 6+ years overall" means 6.
    return max(values) if values else None


# --- remote / location ---------------------------------------------------------------------

_REMOTE = re.compile(
    r"\b(remote|fully[- ]remote|work from home|wfh|удал[её]нн?\w*|remote-first)\b", re.I
)
_HYBRID = re.compile(r"\b(hybrid|гибрид\w*)\b", re.I)
_ONSITE = re.compile(r"\b(on[- ]?site|in[- ]office|офис\w*)\b", re.I)
_RESIDENCY = re.compile(
    r"(?:must|should|need to|required to)\s+(?:be\s+)?(?:based|located|resid\w+|liv\w+)\s+in\s+"
    r"(?:the\s+)?([^.\n;]{2,60})"
    r"|\b(?:remote|open to candidates)\s*[(\-–:]\s*([A-Za-z ,/&]{2,40}?)"
    r"\s*(?:only)?\s*\)?(?=[.\n;]|$)"
    r"|\b([A-Z][A-Za-z.]+(?: [A-Z][a-z]+)?)[- ]only\b"
    r"|authori[sz]ed to work in (?:the\s+)?([^.\n;,]{2,40})",
    re.M,
)


def remote_policy(posting: RawPosting) -> RemotePolicy:
    if posting.remote_hint is not RemotePolicy.UNKNOWN:
        return posting.remote_hint
    head = f"{posting.title}\n{posting.location_raw or ''}"
    for policy, rx in (
        (RemotePolicy.HYBRID, _HYBRID),
        (RemotePolicy.REMOTE, _REMOTE),
        (RemotePolicy.ONSITE, _ONSITE),
    ):
        if rx.search(head):
            return policy
    body = posting.description[:4000]
    if _HYBRID.search(body):
        return RemotePolicy.HYBRID
    if _REMOTE.search(body):
        return RemotePolicy.REMOTE
    return RemotePolicy.UNKNOWN


def remote_regions(posting: RawPosting) -> list[str]:
    regions: list[str] = list(posting.hints.get("remote_regions") or [])
    regions += detect_regions(posting.location_raw)
    for match in _RESIDENCY.finditer(posting.description):
        place = next(g for g in match.groups() if g)
        regions += detect_regions(place) + detect_countries(place)
    if not regions and posting.location_raw:
        regions = detect_countries(posting.location_raw)
    return list(dict.fromkeys(regions))


# --- visa, relocation, citizenship -------------------------------------------------------

_VISA_NO = re.compile(
    r"(no|not|unable to|cannot|can't|won't|will not|do not|does not|don't)\s+(?:\w+\s+){0,3}"
    r"(sponsor\w*|visa)|without (?:the )?(?:need for |requiring )?(?:visa )?sponsorship"
    r"|sponsorship (?:is )?not (?:available|provided|offered)",
    re.I,
)
_VISA_YES = re.compile(
    r"visa sponsorship(?: and [\w ]{1,30}?)?(?: is| are)? (?:available|provided|offered|support)"
    r"|we (?:can |will |do )?sponsor"
    r"|sponsor(?:ship)?\s+(?:\w+\s+){0,3}(?:visas?|work permits?)|visa (?:support|assistance)"
    r"|визов\w* поддержк|оформлени\w+ виз",
    re.I,
)
_RELO_NO = re.compile(
    r"no relocation|relocation (?:is )?not (?:available|provided|offered)"
    r"|без релокации",
    re.I,
)
_RELO_YES = re.compile(
    r"relocation (?:package|support|assistance|bonus|allowance|budget|is provided|provided)"
    r"|(?:help|support|assist)\w* (?:you )?(?:with )?relocat\w+|релокац\w+|переезд\w*",
    re.I,
)
_CITIZENSHIP: list[tuple[str, re.Pattern[str]]] = [
    (
        "SA",
        re.compile(
            r"saudi(?:zation|sation)?\s+nationals?|saudi(?:zation|sation)|nitaqat"
            r"|only (?:open to )?saudi",
            re.I,
        ),
    ),
    (
        "AE",
        re.compile(
            r"emirati(?:sation|zation)?\s+nationals?|emirati(?:sation|zation)"
            r"|uae nationals? only|only (?:open to )?(?:uae nationals|emiratis)",
            re.I,
        ),
    ),
    ("QA", re.compile(r"qatari nationals?|qatari(?:sation|zation)", re.I)),
    ("OM", re.compile(r"omani nationals?|omani(?:sation|zation)", re.I)),
    ("KW", re.compile(r"kuwaiti nationals?|kuwaiti(?:sation|zation)", re.I)),
    (
        "US",
        re.compile(
            r"(?:u\.?s\.?|united states) citizen(?:ship)? (?:is )?(?:required|only)"
            r"|must be a (?:u\.?s\.?|united states) citizen|security clearance"
            r"|\bts/sci\b|itar",
            re.I,
        ),
    ),
    ("JP", re.compile(r"japanese (?:citizenship|nationality) (?:is )?required", re.I)),
]


def tri_state(text: str, yes: re.Pattern[str], no: re.Pattern[str]) -> bool | None:
    if no.search(text):
        return False
    if yes.search(text):
        return True
    return None


# --- languages ---------------------------------------------------------------------------

_LANGS = {
    "ja": r"japanese|日本語",
    "de": r"german|deutsch",
    "fr": r"french|français",
    "ar": r"arabic",
    "ko": r"korean",
    "zh": r"chinese|mandarin|cantonese",
    "pl": r"polish",
    "nl": r"dutch",
    "es": r"spanish",
    "pt": r"portuguese",
    "it": r"italian",
    "tr": r"turkish",
    "ru": r"russian|русск\w+",
    "kk": r"kazakh|казахск\w+|қазақ\w*",
}
_LANG_REQ = (
    r"fluen\w*|native|business[- ]level|proficien\w*|professional working|N1|N2|JLPT|"
    r"required|must|свободн\w*|владени\w*|знание|обязательн\w*"
)
_LANG_SOFT = re.compile(
    r"plus|nice to have|bonus|preferred|advantage|желательн\w*|преимуществ\w*", re.I
)


def required_languages(text: str) -> list[str]:
    found = []
    for code, names in _LANGS.items():
        rx = re.compile(
            rf"(?:(?:{_LANG_REQ})\W+(?:\w+\W+){{0,4}}?(?:{names}))|(?:(?:{names})\W+(?:\w+\W+){{0,3}}?"
            rf"(?:{_LANG_REQ}))",
            re.I,
        )
        for match in rx.finditer(text):
            window = text[max(0, match.start() - 40) : match.end() + 40]
            if not _LANG_SOFT.search(window):
                found.append(code)
                break
    return found


# --- skills ------------------------------------------------------------------------------

SKILLS: dict[str, str] = {
    "python": r"python",
    "go": r"golang|go(?=\s*(?:[,/).]|$)|\s+(?:developer|engineer|lang))",
    "java": r"java(?!script)",
    "kotlin": r"kotlin",
    "typescript": r"typescript|\bts\b",
    "javascript": r"javascript|\bjs\b",
    "rust": r"rust",
    "c++": r"c\+\+|cpp",
    "scala": r"scala",
    "sql": r"sql",
    "react": r"react(?:\.js)?",
    "node.js": r"node(?:\.js|js)?",
    "fastapi": r"fastapi",
    "django": r"django",
    "flask": r"flask",
    "postgresql": r"postgres(?:ql)?",
    "mysql": r"mysql",
    "mongodb": r"mongo(?:db)?",
    "celery": r"celery",
    "redis": r"redis",
    "kafka": r"kafka",
    "rabbitmq": r"rabbitmq",
    "clickhouse": r"clickhouse",
    "elasticsearch": r"elastic(?:search)?",
    "kubernetes": r"kubernetes|k8s",
    "docker": r"docker",
    "terraform": r"terraform",
    "aws": r"aws|amazon web services",
    "gcp": r"gcp|google cloud",
    "azure": r"azure",
    "spark": r"spark|pyspark",
    "airflow": r"airflow",
    "dbt": r"dbt",
    "pytorch": r"pytorch|torch",
    "tensorflow": r"tensorflow",
    "llm": r"llms?|large language model",
    "rag": r"rag|retrieval[- ]augmented",
    "langchain": r"langchain",
    "langgraph": r"langgraph",
    "mcp": r"mcp|model context protocol",
    "vllm": r"vllm",
    "nlp": r"nlp",
    "computer vision": r"computer vision|opencv",
    "mlops": r"mlops",
    "grpc": r"grpc",
    "graphql": r"graphql",
    "pgvector": r"pgvector",
    "qdrant": r"qdrant",
    "linux": r"linux",
    "machine learning": r"machine learning|\bml\b",
    "recsys": r"recommend\w+ system|recsys",
}
_SKILL_RX = {
    name: re.compile(rf"(?<![\w+#.])(?:{pat})(?![\w+#])", re.I) for name, pat in SKILLS.items()
}


def skills(text: str) -> list[str]:
    return [name for name, rx in _SKILL_RX.items() if rx.search(text)]


# --- salary ------------------------------------------------------------------------------

_NUM = r"\d{1,3}(?:[   ,.]\d{3})+|\d+(?:[.,]\d+)?"
_SALARY = re.compile(
    rf"(?:(?P<from>от|from)\s*)?(?P<pre>[$€£]|(?:aed|sar|qar|usd|eur|gbp|kzt|jpy)\b)?\s*(?P<a>{_NUM})\s*(?P<k>[kкK])?\s*"
    rf"(?:(?:-|–|—|до|to)\s*(?P<pre2>[$€£])?\s*(?P<b>{_NUM})\s*(?P<k2>[kкK])?)?\s*"
    r"(?P<cur>₸|тг\.?|тенге|kzt|usd|eur|gbp|руб\.?|rub|aed|sar|qar|jpy|円)?",
    re.I,
)
_CUR_MAP = {
    "₸": "KZT",
    "тг": "KZT",
    "тг.": "KZT",
    "тенге": "KZT",
    "руб": "RUB",
    "руб.": "RUB",
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "円": "JPY",
}


def _num(value: str, k: str | None) -> float:
    digits = re.sub(r"[   ,]", "", value)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):  # "1.200.000"
        digits = digits.replace(".", "")
    number = float(digits)
    return number * 1000 if k else number


def parse_salary_text(text: str | None) -> Salary | None:
    if not text:
        return None
    for m in _SALARY.finditer(text):
        cur_raw = (m.group("pre") or m.group("pre2") or m.group("cur") or "").lower()
        if not cur_raw:
            continue
        currency = canonical_currency(_CUR_MAP.get(cur_raw, cur_raw))
        a = _num(m.group("a"), m.group("k") or (m.group("k2") if m.group("b") else None))
        b = _num(m.group("b"), m.group("k2")) if m.group("b") else None
        if max(a, b or 0) < 300:  # "5 years", "$50 gift card"
            continue
        tail = text[m.end() : m.end() + 30].lower()
        head = text[max(0, m.start() - 30) : m.start()].lower()
        ctx = head + tail
        if re.search(r"month|/mo\b|в месяц|/мес|мес\.|monthly|ежемесяч", ctx):
            period = "month"
        elif re.search(r"year|annual|/yr|в год|годов", ctx):
            period = "year"
        elif re.search(r"hour|/hr|в час", ctx):
            period = "hour"
        elif currency in {"KZT", "RUB"}:
            period = "month"
        else:
            period = "year" if max(a, b or 0) >= 20_000 and currency != "JPY" else "month"
        lo, hi = (a, b) if b is not None else ((a, None) if m.group("from") else (a, a))
        return Salary(min=lo, max=hi, currency=currency, period=period)
    return None


# --- extractor ---------------------------------------------------------------------------


def _hint_seniority(hints: dict[str, Any]) -> Seniority:
    value = str(hints.get("seniority") or "").strip().lower()
    return _HINT_LEVELS.get(value, Seniority.UNKNOWN)


class RuleExtractor:
    name = "rules-v1"

    async def extract(self, posting: RawPosting) -> JobFacts:
        return self.extract_sync(posting)

    def extract_sync(self, posting: RawPosting) -> JobFacts:
        text = f"{posting.title}\n{posting.location_raw or ''}\n{posting.description}"
        hints = posting.hints
        years = hints.get("years_min")
        if years is None:
            years = years_min(posting.description)
        title_level = seniority_from_title(posting.title)
        level = title_level or _hint_seniority(hints)
        # "Senior in disguise": a mid/unlabelled title that asks for 5+ years is a senior role.
        level = max(level, seniority_from_years(years)) if level <= Seniority.MID else level

        countries: list[str] = list(hints.get("countries") or [])
        if posting.country_hint:
            countries.append(posting.country_hint.upper())
        countries += detect_countries(posting.location_raw)
        remote = remote_policy(posting)
        citizenship = [code for code, rx in _CITIZENSHIP if rx.search(text)]
        skill_list = list(
            dict.fromkeys([s.lower() for s in hints.get("skills") or []] + skills(text))
        )
        return JobFacts(
            seniority=level,
            years_min=years,
            remote=remote,
            remote_regions=remote_regions(posting) if remote is not RemotePolicy.ONSITE else [],
            countries=list(dict.fromkeys(countries)),
            visa_sponsorship=tri_state(text, _VISA_YES, _VISA_NO),
            relocation=tri_state(text, _RELO_YES, _RELO_NO),
            citizenship_required=citizenship,
            required_languages=required_languages(text),
            skills=skill_list,
            salary=posting.salary or parse_salary_text(posting.description),
        )
