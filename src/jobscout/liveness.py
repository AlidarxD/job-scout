"""Is this posting still open? Fetch the canonical URL and look for closed signals.

Used by `jobscout verify` so a shortlist can say "N of M links verified open today" instead
of trusting crawl freshness.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from jobscout.domain import Job, JobStatus

_CLOSED = re.compile(
    r"no longer (?:accepting|available|open)|position (?:has been )?filled|job (?:is )?closed"
    r"|this job has expired|posting (?:has )?(?:closed|expired)|not accepting applications"
    r"|вакансия (?:в архиве|закрыта)|вакансия больше не доступна|the page you were looking for",
    re.I,
)


@dataclass
class LivenessResult:
    job_id: str
    status: JobStatus
    http_status: int | None
    reason: str


async def check(client: httpx.AsyncClient, job: Job) -> LivenessResult:
    try:
        resp = await client.get(job.url, headers={"Accept": "text/html"})
    except httpx.HTTPError as exc:
        return LivenessResult(
            job.id, JobStatus.UNKNOWN, None, f"fetch failed: {type(exc).__name__}"
        )
    if resp.status_code in (404, 410):
        return LivenessResult(job.id, JobStatus.CLOSED, resp.status_code, "page gone")
    if resp.status_code >= 400:
        return LivenessResult(job.id, JobStatus.UNKNOWN, resp.status_code, "blocked or error")
    # Greenhouse/Lever redirect closed postings to the board root.
    if job.url.rstrip("/") != str(resp.url).rstrip("/") and _is_board_root(str(resp.url)):
        return LivenessResult(job.id, JobStatus.CLOSED, resp.status_code, "redirected to board")
    if match := _CLOSED.search(resp.text[:200_000]):
        return LivenessResult(job.id, JobStatus.CLOSED, resp.status_code, match.group(0))
    return LivenessResult(job.id, JobStatus.OPEN, resp.status_code, "ok")


def _is_board_root(url: str) -> bool:
    return bool(
        re.search(
            r"(boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com)/[^/]+/?"
            r"(\?.*)?$",
            url,
        )
    )


async def check_many(
    client: httpx.AsyncClient, jobs: list[Job], concurrency: int = 5
) -> list[LivenessResult]:
    sem = asyncio.Semaphore(concurrency)

    async def one(job: Job) -> LivenessResult:
        async with sem:
            return await check(client, job)

    return list(await asyncio.gather(*(one(j) for j in jobs)))
