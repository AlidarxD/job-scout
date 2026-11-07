"""Employer ATS public job-board APIs: direct employer postings, no aggregator in between.

All of these are documented, unauthenticated, read-only endpoints that the ATS vendors
expose precisely so job boards can be embedded elsewhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from jobscout.domain import Directness, RawPosting, RemotePolicy, Salary
from jobscout.http import get_json, parse_dt
from jobscout.normalize.text import html_to_text
from jobscout.sources.base import Source


def remote_from(value: Any) -> RemotePolicy:
    text = str(value or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if text in {"remote", "true", "fullyremote"}:
        return RemotePolicy.REMOTE
    if text == "hybrid":
        return RemotePolicy.HYBRID
    if text in {"onsite", "office", "inoffice", "false"}:
        return RemotePolicy.ONSITE
    return RemotePolicy.UNKNOWN


class _AtsSource(Source):
    directness = Directness.EMPLOYER_ATS

    @property
    def board(self) -> str:
        return str(self.options["board"])

    @property
    def company(self) -> str:
        return str(self.options.get("company") or self.board)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.board}"


# --- Greenhouse --------------------------------------------------------------------------


class GreenhouseSource(_AtsSource):
    kind = "greenhouse"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{self.board}/jobs"
        payload = await get_json(client, url, params={"content": "true"})
        for posting in parse_greenhouse(payload, self.company):
            yield posting


def parse_greenhouse(payload: dict[str, Any], company: str) -> list[RawPosting]:
    out = []
    for job in payload.get("jobs", []):
        location = (job.get("location") or {}).get("name")
        out.append(
            RawPosting(
                source="greenhouse",
                external_id=str(job["id"]),
                url=job["absolute_url"],
                apply_url=job["absolute_url"],
                title=job["title"].strip(),
                company=job.get("company_name") or company,
                description=html_to_text(job.get("content")),
                location_raw=location,
                posted_at=parse_dt(job.get("first_published") or job.get("updated_at")),
                remote_hint=RemotePolicy.REMOTE
                if location and "remote" in location.lower()
                else RemotePolicy.UNKNOWN,
                tags=[d["name"] for d in job.get("departments", []) if d.get("name")],
                directness=Directness.EMPLOYER_ATS,
                raw=job,
            )
        )
    return out


# --- Lever -------------------------------------------------------------------------------

_LEVER_INTERVAL = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}


class LeverSource(_AtsSource):
    kind = "lever"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        host = "api.eu.lever.co" if self.options.get("region") == "eu" else "api.lever.co"
        payload = await get_json(
            client, f"https://{host}/v0/postings/{self.board}", params={"mode": "json"}
        )
        for posting in parse_lever(payload, self.company):
            yield posting


def parse_lever(payload: list[dict[str, Any]], company: str) -> list[RawPosting]:
    out = []
    for job in payload:
        cats = job.get("categories") or {}
        parts = [job.get("descriptionPlain") or ""]
        for block in job.get("lists") or []:
            parts.append(f"{block.get('text', '')}\n{html_to_text(block.get('content'))}")
        parts.append(job.get("additionalPlain") or "")
        salary = None
        if rng := job.get("salaryRange"):
            salary = Salary(
                min=rng.get("min"),
                max=rng.get("max"),
                currency=rng.get("currency") or "USD",
                period=_LEVER_INTERVAL.get(rng.get("interval", ""), "year"),
            )
        out.append(
            RawPosting(
                source="lever",
                external_id=job["id"],
                url=job["hostedUrl"],
                apply_url=job.get("applyUrl"),
                title=job["text"].strip(),
                company=company,
                description="\n\n".join(p.strip() for p in parts if p and p.strip()),
                location_raw=cats.get("location"),
                posted_at=parse_dt(job.get("createdAt")),
                remote_hint=remote_from(job.get("workplaceType")),
                country_hint=(job.get("country") or None),
                salary=salary,
                tags=[t for t in (cats.get("team"), cats.get("commitment")) if t],
                directness=Directness.EMPLOYER_ATS,
                raw=job,
            )
        )
    return out


# --- Ashby -------------------------------------------------------------------------------

_ASHBY_INTERVAL = {"1 YEAR": "year", "1 MONTH": "month", "1 HOUR": "hour", "1 WEEK": "week"}


class AshbySource(_AtsSource):
    kind = "ashby"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{self.board}"
        payload = await get_json(client, url, params={"includeCompensation": "true"})
        for posting in parse_ashby(payload, self.company):
            yield posting


def parse_ashby(payload: dict[str, Any], company: str) -> list[RawPosting]:
    out = []
    for job in payload.get("jobs", []):
        if job.get("isListed") is False:
            continue
        locations = [job.get("location")] + [
            s.get("location") for s in job.get("secondaryLocations") or []
        ]
        location = " / ".join(loc for loc in locations if loc)
        country = ((job.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        remote = remote_from(job.get("workplaceType"))
        if remote is RemotePolicy.UNKNOWN and job.get("isRemote"):
            remote = RemotePolicy.REMOTE
        salary = None
        for comp in (job.get("compensation") or {}).get("summaryComponents") or []:
            if comp.get("compensationType") == "Salary":
                salary = Salary(
                    min=comp.get("minValue"),
                    max=comp.get("maxValue"),
                    currency=comp.get("currencyCode") or "USD",
                    period=_ASHBY_INTERVAL.get(comp.get("interval", ""), "year"),
                )
                break
        out.append(
            RawPosting(
                source="ashby",
                external_id=job["id"],
                url=job["jobUrl"],
                apply_url=job.get("applyUrl") or job["jobUrl"],
                title=job["title"].strip(),
                company=company,
                description=job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")),
                location_raw=location or None,
                posted_at=parse_dt(job.get("publishedAt")),
                remote_hint=remote,
                hints={"countries": [country]} if country and len(country) == 2 else {},
                salary=salary,
                tags=[t for t in (job.get("department"), job.get("team")) if t],
                directness=Directness.EMPLOYER_ATS,
                raw=job,
            )
        )
    return out


# --- Workable ----------------------------------------------------------------------------


class WorkableSource(_AtsSource):
    kind = "workable"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{self.board}"
        payload = await get_json(client, url, params={"details": "true"})
        for posting in parse_workable(payload, self.company):
            yield posting


def parse_workable(payload: dict[str, Any], company: str) -> list[RawPosting]:
    out = []
    name = payload.get("name") or company
    for job in payload.get("jobs", []):
        location = ", ".join(
            p for p in (job.get("city"), job.get("state"), job.get("country")) if p
        )
        codes = [loc["countryCode"] for loc in job.get("locations") or [] if loc.get("countryCode")]
        out.append(
            RawPosting(
                source="workable",
                external_id=job["shortcode"],
                url=job.get("url") or job["shortlink"],
                apply_url=job.get("application_url"),
                title=job["title"].strip(),
                company=name,
                description=html_to_text(job.get("description")),
                location_raw=location or None,
                posted_at=parse_dt(job.get("published_on") or job.get("created_at")),
                remote_hint=RemotePolicy.REMOTE
                if job.get("telecommuting")
                else RemotePolicy.UNKNOWN,
                hints={"countries": codes} if codes else {},
                tags=[t for t in (job.get("department"), job.get("function")) if t],
                directness=Directness.EMPLOYER_ATS,
                raw=job,
            )
        )
    return out


# --- SmartRecruiters ---------------------------------------------------------------------


class SmartRecruitersSource(_AtsSource):
    kind = "smartrecruiters"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        base = f"https://api.smartrecruiters.com/v1/companies/{self.board}/postings"
        offset, limit = 0, 100
        sem = asyncio.Semaphore(4)

        async def detail(ref: str) -> dict[str, Any]:
            async with sem:
                data: dict[str, Any] = await get_json(client, ref)
                return data

        while True:
            page = await get_json(client, base, params={"offset": offset, "limit": limit})
            items = page.get("content", [])
            details = await asyncio.gather(
                *(detail(item["ref"]) for item in items if item.get("ref")),
                return_exceptions=True,
            )
            by_id = {d["id"]: d for d in details if isinstance(d, dict) and "id" in d}
            for posting in parse_smartrecruiters(page, self.board, self.company, by_id):
                yield posting
            offset += limit
            if offset >= page.get("totalFound", 0) or not items:
                break


def parse_smartrecruiters(
    page: dict[str, Any], board: str, company: str, details: dict[str, dict[str, Any]]
) -> list[RawPosting]:
    out = []
    for job in page.get("content", []):
        loc = job.get("location") or {}
        detail = details.get(job["id"], {})
        sections = ((detail.get("jobAd") or {}).get("sections")) or {}
        description = "\n\n".join(
            html_to_text((sections.get(k) or {}).get("text"))
            for k in (
                "companyDescription",
                "jobDescription",
                "qualifications",
                "additionalInformation",
            )
        ).strip()
        country = (loc.get("country") or "").upper()
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{job['id']}"
        out.append(
            RawPosting(
                source="smartrecruiters",
                external_id=str(job["id"]),
                url=url,
                apply_url=detail.get("applyUrl") or url,
                title=job["name"].strip(),
                company=(job.get("company") or {}).get("name") or company,
                description=description,
                location_raw=loc.get("fullLocation")
                or ", ".join(p for p in (loc.get("city"), loc.get("country")) if p),
                posted_at=parse_dt(job.get("releasedDate")),
                remote_hint=RemotePolicy.REMOTE if loc.get("remote") else RemotePolicy.UNKNOWN,
                hints={
                    **({"countries": [country]} if len(country) == 2 else {}),
                    **(
                        {"seniority": job["experienceLevel"]["label"]}
                        if (job.get("experienceLevel") or {}).get("label")
                        else {}
                    ),
                },
                directness=Directness.EMPLOYER_ATS,
                raw=job,
            )
        )
    return out
