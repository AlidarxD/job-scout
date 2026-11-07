"""LinkedIn jobs through an Apify actor.

We never scrape LinkedIn ourselves (ToS); a paid Apify actor does it and we consume its
dataset. Actor output schemas differ between actors and versions, so the mapper accepts the
common key spellings and drops rows that lack a title, company or URL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from jobscout.domain import Directness, RawPosting, RemotePolicy
from jobscout.http import parse_dt, request
from jobscout.normalize.text import html_to_text
from jobscout.sources.base import Source


class LinkedInApifySource(Source):
    """Options: input (dict passed verbatim to the actor, e.g. keywords/location/rows)."""

    kind = "linkedin"
    directness = Directness.AGGREGATOR

    @property
    def key(self) -> str:
        actor_input = self.options.get("input") or {}
        return f"linkedin:{actor_input.get('keywords') or actor_input.get('title') or 'default'}"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        token = self.settings.apify_token
        if token is None:
            return
        actor = self.options.get("actor") or self.settings.apify_linkedin_actor
        resp = await request(
            client,
            "POST",
            f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
            json=self.options.get("input") or {},
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=300,
            attempts=1,  # runs cost money; never retry blindly
        )
        for posting in parse_linkedin_items(resp.json()):
            yield posting


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def parse_linkedin_items(rows: list[dict[str, Any]]) -> list[RawPosting]:
    out = []
    for row in rows:
        title = _first(row, "job_title", "title", "jobTitle", "position")
        company = _first(row, "company_name", "companyName", "company")
        if isinstance(company, dict):
            company = company.get("name")
        url = _first(row, "job_url", "jobUrl", "link", "url")
        if not (title and company and url):
            continue
        url = str(url).split("?")[0]
        ext_id = str(_first(row, "job_id", "jobId", "id") or url.rstrip("/").rsplit("-", 1)[-1])
        location = _first(row, "location", "job_location", "jobLocation")
        workplace = str(_first(row, "workplace_type", "workplaceType", "work_type") or "").lower()
        remote = (
            RemotePolicy.REMOTE
            if "remote" in workplace or "remote" in str(location).lower()
            else RemotePolicy.HYBRID
            if "hybrid" in workplace
            else RemotePolicy.ONSITE
            if "site" in workplace
            else RemotePolicy.UNKNOWN
        )
        description = _first(
            row, "description", "job_description", "descriptionText", "descriptionHtml"
        )
        hints: dict[str, Any] = {}
        if level := _first(row, "seniority_level", "seniorityLevel", "experienceLevel"):
            hints["seniority"] = level
        out.append(
            RawPosting(
                source="linkedin",
                external_id=ext_id,
                url=url,
                apply_url=_first(row, "apply_url", "applyUrl", "external_apply_url"),
                title=str(title).strip(),
                company=str(company).strip(),
                description=html_to_text(str(description or "")),
                location_raw=str(location) if location else None,
                posted_at=parse_dt(
                    _first(row, "posted_at", "postedAt", "publishedAt", "listed_at", "postedDate")
                ),
                remote_hint=remote,
                hints=hints,
                directness=Directness.AGGREGATOR,
                raw=row,
            )
        )
    return out
