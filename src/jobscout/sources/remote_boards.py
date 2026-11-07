"""Global remote-job boards with public JSON feeds.

Terms worth respecting (also enforced by the default schedule in config/sources.yaml):
- Remotive: link back to the original posting, at most a few requests per day.
- RemoteOK: link back to remoteok.com and mention it as the source.
- Arbeitnow / Himalayas: public, rate-limit politely.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jobscout.domain import Directness, RawPosting, RemotePolicy, Salary
from jobscout.http import get_json, parse_dt
from jobscout.normalize.geo import detect_countries, detect_regions
from jobscout.normalize.text import html_to_text
from jobscout.sources.base import Source


def _region_hints(text: str | None) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    if regions := detect_regions(text) + detect_countries(text):
        hints["remote_regions"] = regions
    return hints


# --- Remotive ----------------------------------------------------------------------------


class RemotiveSource(Source):
    kind = "remotive"

    @property
    def key(self) -> str:
        return f"remotive:{self.options.get('category', 'all')}"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        params = {k: v for k, v in self.options.items() if k in {"category", "search", "limit"}}
        payload = await get_json(client, "https://remotive.com/api/remote-jobs", params=params)
        for posting in parse_remotive(payload):
            yield posting


_MONEY = re.compile(r"(\$|€|£)?\s*(\d[\d,.]*)\s*(k)?", re.I)


def parse_money_range(text: str | None) -> Salary | None:
    """'$120k - $150k', '€60,000–80,000', '100000 USD' → Salary (yearly by default)."""
    if not text:
        return None
    nums: list[float] = []
    symbol = None
    for sym, num, k in _MONEY.findall(text):
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        value = value * 1000 if k else value
        if value >= 1000:
            nums.append(value)
            symbol = symbol or sym
    if not nums:
        return None
    currency = {"€": "EUR", "£": "GBP"}.get(symbol or "", "USD")
    for code in ("EUR", "GBP", "USD", "CAD", "AUD", "CHF", "PLN"):
        if code in text.upper():
            currency = code
    low = text.lower()
    period = "month" if ("month" in low or "/mo" in low) else "hour" if "hour" in low else "year"
    return Salary(min=min(nums), max=max(nums), currency=currency, period=period)


def parse_remotive(payload: dict[str, Any]) -> list[RawPosting]:
    out = []
    for job in payload.get("jobs", []):
        where = job.get("candidate_required_location")
        out.append(
            RawPosting(
                source="remotive",
                external_id=str(job["id"]),
                url=job["url"],
                title=job["title"].strip(),
                company=job["company_name"].strip(),
                description=html_to_text(job.get("description")),
                location_raw=where,
                posted_at=parse_dt(job.get("publication_date")),
                remote_hint=RemotePolicy.REMOTE,
                salary=parse_money_range(job.get("salary")),
                tags=list(job.get("tags") or []),
                hints=_region_hints(where),
                directness=Directness.AGGREGATOR,
                raw=job,
            )
        )
    return out


# --- RemoteOK ----------------------------------------------------------------------------


class RemoteOkSource(Source):
    kind = "remoteok"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        payload = await get_json(client, "https://remoteok.com/api")
        for posting in parse_remoteok(payload):
            yield posting


def parse_remoteok(payload: list[dict[str, Any]]) -> list[RawPosting]:
    out = []
    for job in payload:
        if "position" not in job:  # first element is the legal notice
            continue
        salary = None
        if job.get("salary_min") or job.get("salary_max"):
            salary = Salary(min=job.get("salary_min") or None, max=job.get("salary_max") or None)
        where = job.get("location") or None
        out.append(
            RawPosting(
                source="remoteok",
                external_id=str(job["id"]),
                url=job.get("url") or f"https://remoteok.com/remote-jobs/{job['id']}",
                apply_url=job.get("apply_url"),
                title=job["position"].strip(),
                company=job["company"].strip(),
                description=html_to_text(job.get("description")),
                location_raw=where,
                posted_at=parse_dt(job.get("epoch") or job.get("date")),
                remote_hint=RemotePolicy.REMOTE,
                salary=salary,
                tags=list(job.get("tags") or []),
                hints=_region_hints(where),
                directness=Directness.AGGREGATOR,
                raw=job,
            )
        )
    return out


# --- Arbeitnow (Europe, mostly Germany) ---------------------------------------------------


class ArbeitnowSource(Source):
    kind = "arbeitnow"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        url: str | None = "https://www.arbeitnow.com/api/job-board-api"
        for _ in range(int(self.options.get("max_pages", 5))):
            if not url:
                break
            payload = await get_json(client, url)
            for posting in parse_arbeitnow(payload):
                yield posting
            url = (payload.get("links") or {}).get("next")


def parse_arbeitnow(payload: dict[str, Any]) -> list[RawPosting]:
    out = []
    for job in payload.get("data", []):
        out.append(
            RawPosting(
                source="arbeitnow",
                external_id=job["slug"],
                url=job["url"],
                title=job["title"].strip(),
                company=job["company_name"].strip(),
                description=html_to_text(job.get("description")),
                location_raw=job.get("location"),
                posted_at=parse_dt(job.get("created_at")),
                remote_hint=RemotePolicy.REMOTE if job.get("remote") else RemotePolicy.UNKNOWN,
                tags=list(job.get("tags") or []) + list(job.get("job_types") or []),
                directness=Directness.AGGREGATOR,
                raw=job,
            )
        )
    return out


# --- Himalayas ---------------------------------------------------------------------------


class HimalayasSource(Source):
    kind = "himalayas"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        limit = 20
        for page in range(int(self.options.get("max_pages", 10))):
            payload = await get_json(
                client,
                "https://himalayas.app/jobs/api",
                params={"limit": limit, "offset": page * limit},
            )
            postings = parse_himalayas(payload)
            for posting in postings:
                yield posting
            if len(payload.get("jobs", [])) < limit:
                break


def parse_himalayas(payload: dict[str, Any]) -> list[RawPosting]:
    out = []
    for job in payload.get("jobs", []):
        restrictions = job.get("locationRestrictions") or []
        where = ", ".join(restrictions) if restrictions else "Worldwide"
        hints = _region_hints(where)
        if seniority := job.get("seniority"):
            hints["seniority"] = seniority[0] if isinstance(seniority, list) else seniority
        salary = None
        if job.get("minSalary") or job.get("maxSalary"):
            salary = Salary(
                min=job.get("minSalary"),
                max=job.get("maxSalary"),
                currency=job.get("currency") or "USD",
            )
        out.append(
            RawPosting(
                source="himalayas",
                external_id=str(job.get("guid") or job["applicationLink"]),
                url=job.get("guid") or job["applicationLink"],
                apply_url=job.get("applicationLink"),
                title=job["title"].strip(),
                company=job["companyName"].strip(),
                description=html_to_text(job.get("description") or job.get("excerpt")),
                location_raw=where,
                posted_at=parse_dt(job.get("pubDate")),
                remote_hint=RemotePolicy.REMOTE,
                salary=salary,
                tags=list(job.get("categories") or []),
                hints=hints,
                directness=Directness.AGGREGATOR,
                raw=job,
            )
        )
    return out
