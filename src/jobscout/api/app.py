from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jobscout.domain import Job, JobStatus, RemotePolicy, Scope, Seniority
from jobscout.pipeline import score_all
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import ScoreResult
from jobscout.settings import Settings, get_settings
from jobscout.storage.base import Application, ApplicationStatus, JobFilter, Repository, Stats

STATIC = Path(__file__).parent / "static"


class JobOut(BaseModel):
    id: str
    title: str
    company: str
    url: str
    apply_url: str | None
    location: str | None
    scope: Scope
    status: JobStatus
    remote: RemotePolicy
    remote_regions: list[str]
    countries: list[str]
    seniority: str
    years_min: int | None
    visa_sponsorship: bool | None
    relocation: bool | None
    required_languages: list[str]
    citizenship_required: list[str]
    skills: list[str]
    salary_usd_month: tuple[float | None, float | None] | None
    posted_at: datetime | None
    age_days: int | None
    sources: list[str]

    @classmethod
    def of(cls, job: Job, now: datetime) -> JobOut:
        f = job.facts
        return cls(
            id=job.id,
            title=job.title,
            company=job.company,
            url=job.url,
            apply_url=job.apply_url,
            location=job.location_raw,
            scope=job.scope,
            status=job.status,
            remote=f.remote,
            remote_regions=f.remote_regions,
            countries=f.countries,
            seniority=f.seniority.label,
            years_min=f.years_min,
            visa_sponsorship=f.visa_sponsorship,
            relocation=f.relocation,
            required_languages=f.required_languages,
            citizenship_required=f.citizenship_required,
            skills=f.skills,
            salary_usd_month=f.salary.monthly_usd() if f.salary else None,
            posted_at=job.posted_at,
            age_days=job.age_days(now),
            sources=sorted({s.source for s in job.sources}),
        )


class JobDetail(JobOut):
    description: str


class MatchOut(BaseModel):
    job: JobOut
    score: ScoreResult


class ApplicationIn(BaseModel):
    status: ApplicationStatus
    notes: str = ""


def get_repo(request: Request) -> Repository:
    repository: Repository = request.app.state.repo
    return repository


RepoDep = Annotated[Repository, Depends(get_repo)]


def _build_repo(settings: Settings) -> Repository:
    if settings.storage == "memory":
        from jobscout.storage.memory import InMemoryRepository

        return InMemoryRepository()
    from jobscout.storage.sql import SqlRepository

    return SqlRepository.from_url(settings.database_url)


def create_app(repo: Repository | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.repo = repo or _build_repo(settings)
        yield
        close = getattr(app.state.repo, "close", None)
        if close:
            await close()

    app = FastAPI(title="Job Scout", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/jobs", response_model=list[JobOut])
    async def list_jobs(
        repo: RepoDep,
        scope: Scope | None = None,
        country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
        remote: RemotePolicy | None = None,
        seniority: str | None = None,
        source: str | None = None,
        q: Annotated[str | None, Query(max_length=100)] = None,
        days: Annotated[int | None, Query(ge=1, le=365)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[JobOut]:
        level = None
        if seniority:
            try:
                level = Seniority[seniority.upper()]
            except KeyError as exc:
                raise HTTPException(422, f"unknown seniority {seniority!r}") from exc
        flt = JobFilter(
            scope=scope,
            country=country,
            remote=remote,
            seniority=level,
            source=source,
            q=q,
            posted_within_days=days,
            limit=limit,
            offset=offset,
        )
        now = datetime.now(UTC)
        return [JobOut.of(j, now) for j in await repo.list_jobs(flt)]

    @app.get("/api/jobs/{job_id}", response_model=JobDetail)
    async def get_job(job_id: str, repo: RepoDep) -> JobDetail:
        job = await repo.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        base = JobOut.of(job, datetime.now(UTC))
        return JobDetail(**base.model_dump(), description=job.description)

    @app.get("/api/stats", response_model=Stats)
    async def stats(repo: RepoDep) -> Stats:
        return await repo.stats()

    @app.put("/api/profiles/{profile_id}", response_model=CandidateProfile)
    async def put_profile(
        profile_id: str, profile: CandidateProfile, repo: RepoDep
    ) -> CandidateProfile:
        profile = profile.model_copy(update={"id": profile_id})
        await repo.save_profile(profile)
        return profile

    @app.get("/api/profiles/{profile_id}", response_model=CandidateProfile)
    async def get_profile(profile_id: str, repo: RepoDep) -> CandidateProfile:
        profile = await repo.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "profile not found")
        return profile

    @app.post("/api/profiles/{profile_id}/score")
    async def rescore(profile_id: str, repo: RepoDep) -> dict[str, Any]:
        profile = await get_profile(profile_id, repo)
        results = await score_all(repo, profile)
        return {
            "scored": len(results),
            "passed": sum(r.passed for r in results),
            "rejected": sum(bool(r.gates) for r in results),
        }

    @app.get("/api/profiles/{profile_id}/matches", response_model=list[MatchOut])
    async def matches(
        profile_id: str,
        repo: RepoDep,
        include_rejected: bool = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> list[MatchOut]:
        now = datetime.now(UTC)
        rows = await repo.list_scores(profile_id, include_rejected=include_rejected, limit=limit)
        return [MatchOut(job=JobOut.of(job, now), score=res) for job, res in rows]

    @app.put("/api/profiles/{profile_id}/applications/{job_id}", response_model=Application)
    async def put_application(
        profile_id: str, job_id: str, body: ApplicationIn, repo: RepoDep
    ) -> Application:
        if await repo.get_job(job_id) is None:
            raise HTTPException(404, "job not found")
        if await repo.get_profile(profile_id) is None:
            raise HTTPException(404, "profile not found")
        return await repo.upsert_application(
            Application(profile_id=profile_id, job_id=job_id, status=body.status, notes=body.notes)
        )

    @app.get("/api/profiles/{profile_id}/applications", response_model=list[Application])
    async def list_applications(profile_id: str, repo: RepoDep) -> list[Application]:
        return await repo.list_applications(profile_id)

    return app
