"""In-process repository: tests, local demos and `--storage memory` runs."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from jobscout.domain import Job, JobStatus, Seniority
from jobscout.normalize.dedup import normalize_company
from jobscout.normalize.geo import country_in_regions
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import ScoreResult
from jobscout.storage.base import Application, CrawlRun, JobFilter, Stats


def matches(job: Job, flt: JobFilter, now: datetime | None = None) -> bool:
    f = job.facts
    if flt.status and job.status is not flt.status:
        return False
    if flt.scope and job.scope is not flt.scope:
        return False
    if flt.remote and f.remote is not flt.remote:
        return False
    if flt.seniority and f.seniority is not Seniority.UNKNOWN and f.seniority is not flt.seniority:
        return False
    if flt.source and all(s.source != flt.source for s in job.sources):
        return False
    if flt.country:
        code = flt.country.upper()
        in_office = code in f.countries
        in_remote = (
            f.remote.value == "remote"
            and bool(f.remote_regions)
            and country_in_regions(code, f.remote_regions)
        )
        if not (in_office or in_remote):
            return False
    if flt.q:
        needle = flt.q.lower()
        if needle not in job.title.lower() and needle not in job.company.lower():
            return False
    if flt.posted_within_days is not None:
        now = now or datetime.now(UTC)
        ref = job.posted_at or job.first_seen
        if ref < now - timedelta(days=flt.posted_within_days):
            return False
    return True


def sort_key(job: Job) -> datetime:
    return job.posted_at or job.first_seen


class InMemoryRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.extraction: dict[str, tuple[str, str]] = {}
        self.runs: list[CrawlRun] = []
        self.profiles: dict[str, CandidateProfile] = {}
        self.scores: dict[tuple[str, str], ScoreResult] = {}
        self.apps: dict[tuple[str, str], Application] = {}
        self.history: list[tuple[str, JobStatus, str, datetime]] = []

    async def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def upsert_job(self, job: Job, *, desc_hash: str, extractor: str) -> None:
        prev = self.jobs.get(job.id)
        if prev is None or prev.status is not job.status:
            self.history.append((job.id, job.status, "upsert", datetime.now(UTC)))
        self.jobs[job.id] = job
        self.extraction[job.id] = (desc_hash, extractor)

    async def job_extraction_key(self, job_id: str) -> tuple[str, str] | None:
        return self.extraction.get(job_id)

    async def list_jobs(self, flt: JobFilter) -> list[Job]:
        found = sorted(
            (j for j in self.jobs.values() if matches(j, flt)), key=sort_key, reverse=True
        )
        return found[flt.offset : flt.offset + flt.limit]

    async def set_status(self, job_id: str, status: JobStatus, reason: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.status is not status:
            self.jobs[job_id] = job.model_copy(update={"status": status})
            self.history.append((job_id, status, reason, datetime.now(UTC)))

    async def close_missing(
        self, source: str, company: str, seen_external_ids: set[str], *, reason: str
    ) -> int:
        closed = 0
        norm = normalize_company(company)
        for job in list(self.jobs.values()):
            if job.status is not JobStatus.OPEN or normalize_company(job.company) != norm:
                continue
            refs = [s for s in job.sources if s.source == source]
            if refs and all(r.external_id not in seen_external_ids for r in refs):
                await self.set_status(job.id, JobStatus.CLOSED, reason)
                closed += 1
        return closed

    async def close_stale(self, before: datetime) -> int:
        stale = [
            j.id for j in self.jobs.values() if j.status is JobStatus.OPEN and j.last_seen < before
        ]
        for job_id in stale:
            await self.set_status(job_id, JobStatus.CLOSED, "not seen by any crawl")
        return len(stale)

    async def record_run(self, run: CrawlRun) -> None:
        self.runs.append(run)

    async def stats(self) -> Stats:
        open_jobs = [j for j in self.jobs.values() if j.status is JobStatus.OPEN]
        return Stats(
            total_open=len(open_jobs),
            by_scope=dict(Counter(j.scope.value for j in open_jobs)),
            by_country=dict(Counter(j.primary_country or "REMOTE/NA" for j in open_jobs)),
            by_source=dict(Counter(s.source for j in open_jobs for s in j.sources)),
            last_runs=self.runs[-20:],
        )

    async def save_profile(self, profile: CandidateProfile) -> None:
        self.profiles[profile.id] = profile

    async def get_profile(self, profile_id: str) -> CandidateProfile | None:
        return self.profiles.get(profile_id)

    async def save_scores(self, results: list[ScoreResult]) -> None:
        for r in results:
            self.scores[(r.profile_id, r.job_id)] = r

    async def list_scores(
        self, profile_id: str, *, include_rejected: bool, limit: int
    ) -> list[tuple[Job, ScoreResult]]:
        rows = [
            (self.jobs[job_id], r)
            for (pid, job_id), r in self.scores.items()
            if pid == profile_id and job_id in self.jobs and (include_rejected or not r.gates)
        ]
        rows.sort(key=lambda row: (row[1].score, sort_key(row[0])), reverse=True)
        return rows[:limit]

    async def upsert_application(self, app: Application) -> Application:
        stored = app.model_copy(update={"updated_at": datetime.now(UTC)})
        self.apps[(app.profile_id, app.job_id)] = stored
        return stored

    async def list_applications(self, profile_id: str) -> list[Application]:
        return [a for (pid, _), a in self.apps.items() if pid == profile_id]
