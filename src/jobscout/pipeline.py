"""Ingest pipeline: fetch → map → dedup/merge → extract (only when content changed) → store.

Each source runs independently; one failing board never fails the crawl, it is recorded in
crawl_runs with its error so dashboards show source health.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from jobscout.domain import Directness, Job, JobStatus, RawPosting, RemotePolicy, Scope, SourceRef
from jobscout.extract import Extractor
from jobscout.normalize.dedup import fingerprint, merge
from jobscout.normalize.geo import detect_countries
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import ScoreResult, score
from jobscout.sources.base import Source
from jobscout.storage.base import CrawlRun, JobFilter, Repository

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    runs: list[CrawlRun] = field(default_factory=list)
    closed_missing: int = 0
    closed_stale: int = 0

    @property
    def fetched(self) -> int:
        return sum(r.fetched for r in self.runs)

    @property
    def new(self) -> int:
        return sum(r.new for r in self.runs)


def placement(posting: RawPosting) -> str | None:
    """The 'where' part of the dedup fingerprint."""
    countries = list(posting.hints.get("countries") or [])
    if posting.country_hint:
        countries.insert(0, posting.country_hint.upper())
    countries += detect_countries(posting.location_raw)
    if countries:
        return str(countries[0])
    return "REMOTE" if posting.remote_hint is RemotePolicy.REMOTE else None


def content_hash(posting: RawPosting) -> str:
    blob = f"{posting.title}\n{posting.location_raw}\n{posting.description}"
    return hashlib.sha1(blob.encode(), usedforsecurity=False).hexdigest()


def decide_scope(job: Job, source_scope: Scope) -> Scope:
    if source_scope is Scope.LOCAL or "KZ" in job.facts.countries:
        return Scope.LOCAL
    return Scope.GLOBAL


class Ingestor:
    def __init__(self, repo: Repository, extractor: Extractor, *, extract_concurrency: int = 8):
        self.repo = repo
        self.extractor = extractor
        self._sem = asyncio.Semaphore(extract_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}

    async def ingest_posting(self, posting: RawPosting, source_scope: Scope) -> str:
        """Returns 'new' | 'updated' | 'unchanged'."""
        job_id = fingerprint(posting.company, posting.title, placement(posting))
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        async with lock:  # two sources may deliver the same vacancy concurrently
            existing = await self.repo.get_job(job_id)
            digest = content_hash(posting)
            if existing is None:
                job = Job(
                    id=job_id,
                    title=posting.title,
                    company=posting.company,
                    url=posting.url,
                    apply_url=posting.apply_url,
                    description=posting.description,
                    location_raw=posting.location_raw,
                    posted_at=posting.posted_at,
                    tags=sorted(set(posting.tags)),
                    sources=[
                        SourceRef(
                            source=posting.source,
                            external_id=posting.external_id,
                            url=posting.url,
                            directness=posting.directness,
                        )
                    ],
                )
                outcome = "new"
                needs_extract = True
            else:
                job = merge(existing, posting)
                if job.status is JobStatus.CLOSED:
                    job = job.model_copy(update={"status": JobStatus.OPEN})
                outcome = "updated"
                prev = await self.repo.job_extraction_key(job_id)
                # Re-extract only if the canonical text changed or the extractor did.
                canonical = job.url == posting.url
                needs_extract = (
                    prev is None
                    or prev[1] != self.extractor.name
                    or (canonical and prev[0] != digest)
                )
                if not needs_extract and prev:
                    digest = prev[0]
            if needs_extract:
                async with self._sem:
                    facts = await self.extractor.extract(posting)
                job = job.model_copy(update={"facts": facts})
            job = job.model_copy(update={"scope": decide_scope(job, source_scope)})
            await self.repo.upsert_job(job, desc_hash=digest, extractor=self.extractor.name)
            return outcome

    async def run_source(self, source: Source, client: httpx.AsyncClient) -> CrawlRun:
        run = CrawlRun(source_key=source.key, started_at=datetime.now(UTC))
        seen: set[str] = set()
        try:
            async for posting in source.fetch(client):
                run.fetched += 1
                seen.add(posting.external_id)
                outcome = await self.ingest_posting(posting, source.scope)
                run.new += outcome == "new"
                run.updated += outcome == "updated"
        except Exception as exc:
            log.exception("source %s failed", source.key)
            run.error = f"{type(exc).__name__}: {exc}"[:500]
        run.finished_at = datetime.now(UTC)
        await self.repo.record_run(run)
        # An employer ATS board lists every open role, so absence means closed.
        if run.error is None and source.directness is Directness.EMPLOYER_ATS and run.fetched:
            company = str(source.options.get("company") or source.options.get("board"))
            closed = await self.repo.close_missing(
                source.kind, company, seen, reason=f"removed from {source.key}"
            )
            if closed:
                log.info("%s: %d postings closed", source.key, closed)
        return run

    async def run(
        self,
        sources: list[Source],
        client: httpx.AsyncClient,
        *,
        stale_after_days: int = 21,
        source_concurrency: int = 6,
    ) -> IngestReport:
        report = IngestReport()
        sem = asyncio.Semaphore(source_concurrency)

        async def one(src: Source) -> CrawlRun:
            async with sem:
                return await self.run_source(src, client)

        report.runs = list(await asyncio.gather(*(one(s) for s in sources)))
        cutoff = datetime.now(UTC) - timedelta(days=stale_after_days)
        report.closed_stale = await self.repo.close_stale(cutoff)
        return report


async def score_all(
    repo: Repository, profile: CandidateProfile, *, batch: int = 500
) -> list[ScoreResult]:
    await repo.save_profile(profile)
    results: list[ScoreResult] = []
    offset = 0
    while True:
        jobs = await repo.list_jobs(JobFilter(limit=batch, offset=offset))
        if not jobs:
            break
        chunk = [score(job, profile) for job in jobs]
        await repo.save_scores(chunk)
        results += chunk
        offset += batch
    return results
