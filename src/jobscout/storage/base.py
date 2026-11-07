from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from jobscout.domain import Job, JobStatus, RemotePolicy, Scope, Seniority
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import ScoreResult


class ApplicationStatus(StrEnum):
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Application(BaseModel):
    profile_id: str
    job_id: str
    status: ApplicationStatus
    notes: str = ""
    updated_at: datetime | None = None


class JobFilter(BaseModel):
    scope: Scope | None = None
    country: str | None = None  # ISO2; matches office country or remote region
    remote: RemotePolicy | None = None
    seniority: Seniority | None = None
    source: str | None = None
    q: str | None = None
    posted_within_days: int | None = None
    status: JobStatus | None = JobStatus.OPEN
    limit: int = Field(default=50, le=500)
    offset: int = 0


class CrawlRun(BaseModel):
    source_key: str
    started_at: datetime
    finished_at: datetime | None = None
    fetched: int = 0
    new: int = 0
    updated: int = 0
    error: str | None = None


class Stats(BaseModel):
    total_open: int
    by_scope: dict[str, int]
    by_country: dict[str, int]
    by_source: dict[str, int]
    last_runs: list[CrawlRun]


class Repository(Protocol):
    async def get_job(self, job_id: str) -> Job | None: ...
    async def upsert_job(self, job: Job, *, desc_hash: str, extractor: str) -> None: ...
    async def job_extraction_key(self, job_id: str) -> tuple[str, str] | None: ...
    async def list_jobs(self, flt: JobFilter) -> list[Job]: ...
    async def close_missing(
        self, source: str, company: str, seen_external_ids: set[str], *, reason: str
    ) -> int: ...
    async def close_stale(self, before: datetime) -> int: ...
    async def set_status(self, job_id: str, status: JobStatus, reason: str) -> None: ...
    async def record_run(self, run: CrawlRun) -> None: ...
    async def stats(self) -> Stats: ...

    async def save_profile(self, profile: CandidateProfile) -> None: ...
    async def get_profile(self, profile_id: str) -> CandidateProfile | None: ...
    async def save_scores(self, results: list[ScoreResult]) -> None: ...
    async def list_scores(
        self, profile_id: str, *, include_rejected: bool, limit: int
    ) -> list[tuple[Job, ScoreResult]]: ...

    async def upsert_application(self, app: Application) -> Application: ...
    async def list_applications(self, profile_id: str) -> list[Application]: ...
