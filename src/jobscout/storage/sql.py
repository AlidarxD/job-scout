"""Postgres repository (SQLAlchemy 2.0 async + asyncpg)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobscout.domain import Job, JobFacts, JobStatus, Scope, SourceRef
from jobscout.normalize.dedup import normalize_company
from jobscout.normalize.geo import regions_open_to
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import ScoreResult
from jobscout.storage.base import Application, ApplicationStatus, CrawlRun, JobFilter, Stats

EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text)
    company_norm: Mapped[str] = mapped_column(Text, index=True)
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    location_raw: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen: Mapped[datetime] = _ts()
    last_seen: Mapped[datetime] = _ts()
    status: Mapped[str] = mapped_column(String(16), index=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # Denormalised facts for filtering; the full object lives in `facts`.
    countries: Mapped[list[str]] = mapped_column(ARRAY(String(2)), default=list)
    remote: Mapped[str] = mapped_column(String(16), index=True)
    remote_regions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    seniority: Mapped[int] = mapped_column(Integer, index=True)
    salary_min_usd_month: Mapped[float | None] = mapped_column(Float)
    salary_max_usd_month: Mapped[float | None] = mapped_column(Float)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB)
    desc_hash: Mapped[str] = mapped_column(String(40))
    extractor: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    search: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(company,''))",
            persisted=True,
        ),
    )

    __table_args__ = (
        Index("ix_jobs_countries", "countries", postgresql_using="gin"),
        Index("ix_jobs_search", "search", postgresql_using="gin"),
    )


class JobSourceRow(Base):
    __tablename__ = "job_sources"
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    directness: Mapped[int] = mapped_column(Integer)
    last_seen: Mapped[datetime] = _ts()


class JobStatusHistoryRow(Base):
    __tablename__ = "job_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = _ts()


class CrawlRunRow(Base):
    __tablename__ = "crawl_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(Text, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ProfileRow(Base):
    __tablename__ = "profiles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = _ts()


class ScoreRow(Base):
    __tablename__ = "scores"
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[float] = mapped_column(Float, index=True)
    gated: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    scored_at: Mapped[datetime] = _ts()


class ApplicationRow(Base):
    __tablename__ = "applications"
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = _ts()


# --- mapping -----------------------------------------------------------------------------


def _to_job(row: JobRow, refs: list[JobSourceRow]) -> Job:
    return Job(
        id=row.id,
        title=row.title,
        company=row.company,
        url=row.url,
        apply_url=row.apply_url,
        description=row.description,
        location_raw=row.location_raw,
        posted_at=row.posted_at,
        first_seen=row.first_seen,
        last_seen=row.last_seen,
        status=JobStatus(row.status),
        scope=Scope(row.scope),
        tags=list(row.tags or []),
        facts=JobFacts.model_validate(row.facts),
        sources=[
            SourceRef(
                source=r.source, external_id=r.external_id, url=r.url, directness=r.directness
            )
            for r in refs
        ],
    )


def _job_values(job: Job, desc_hash: str, extractor: str) -> dict[str, Any]:
    lo, hi = job.facts.salary.monthly_usd() if job.facts.salary else (None, None)
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company,
        "company_norm": normalize_company(job.company),
        "url": job.url,
        "apply_url": job.apply_url,
        "description": job.description,
        "location_raw": job.location_raw,
        "posted_at": job.posted_at,
        "first_seen": job.first_seen,
        "last_seen": job.last_seen,
        "status": job.status.value,
        "scope": job.scope.value,
        "tags": job.tags,
        "countries": job.facts.countries,
        "remote": job.facts.remote.value,
        "remote_regions": job.facts.remote_regions,
        "seniority": int(job.facts.seniority),
        "salary_min_usd_month": lo,
        "salary_max_usd_month": hi,
        "facts": job.facts.model_dump(mode="json"),
        "desc_hash": desc_hash,
        "extractor": extractor,
    }


class SqlRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, url: str) -> SqlRepository:
        return cls(create_async_engine(url, pool_pre_ping=True))

    async def close(self) -> None:
        await self.engine.dispose()

    async def _refs(self, job_ids: list[str]) -> dict[str, list[JobSourceRow]]:
        if not job_ids:
            return {}
        async with self.session() as s:
            rows = (
                await s.scalars(select(JobSourceRow).where(JobSourceRow.job_id.in_(job_ids)))
            ).all()
        out: dict[str, list[JobSourceRow]] = {}
        for r in rows:
            out.setdefault(r.job_id, []).append(r)
        return out

    async def get_job(self, job_id: str) -> Job | None:
        async with self.session() as s:
            row = await s.get(JobRow, job_id)
        if row is None:
            return None
        return _to_job(row, (await self._refs([job_id])).get(job_id, []))

    async def job_extraction_key(self, job_id: str) -> tuple[str, str] | None:
        async with self.session() as s:
            row = (
                await s.execute(
                    select(JobRow.desc_hash, JobRow.extractor).where(JobRow.id == job_id)
                )
            ).first()
        return (row[0], row[1]) if row else None

    async def upsert_job(self, job: Job, *, desc_hash: str, extractor: str) -> None:
        values = _job_values(job, desc_hash, extractor)
        async with self.session.begin() as s:
            prev = await s.scalar(select(JobRow.status).where(JobRow.id == job.id))
            stmt = insert(JobRow).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[JobRow.id],
                set_={k: stmt.excluded[k] for k in values if k not in {"id", "first_seen"}},
            )
            await s.execute(stmt)
            for ref in job.sources:
                src = insert(JobSourceRow).values(
                    source=ref.source,
                    external_id=ref.external_id,
                    job_id=job.id,
                    url=ref.url,
                    directness=int(ref.directness),
                    last_seen=job.last_seen,
                )
                await s.execute(
                    src.on_conflict_do_update(
                        index_elements=[JobSourceRow.source, JobSourceRow.external_id],
                        set_={"job_id": job.id, "url": ref.url, "last_seen": job.last_seen},
                    )
                )
            if prev != job.status.value:
                s.add(
                    JobStatusHistoryRow(
                        job_id=job.id,
                        status=job.status.value,
                        reason="first seen" if prev is None else "re-opened",
                    )
                )

    async def list_jobs(self, flt: JobFilter) -> list[Job]:
        q = select(JobRow)
        if flt.status:
            q = q.where(JobRow.status == flt.status.value)
        if flt.scope:
            q = q.where(JobRow.scope == flt.scope.value)
        if flt.remote:
            q = q.where(JobRow.remote == flt.remote.value)
        if flt.seniority:
            q = q.where(or_(JobRow.seniority == int(flt.seniority), JobRow.seniority == 0))
        if flt.country:
            code = flt.country.upper()
            regions = regions_open_to(code)
            q = q.where(
                or_(
                    JobRow.countries.contains([code]),  # uses the GIN index
                    (JobRow.remote == "remote") & JobRow.remote_regions.overlap(regions),
                )
            )
        if flt.source:
            q = q.where(
                JobRow.id.in_(select(JobSourceRow.job_id).where(JobSourceRow.source == flt.source))
            )
        if flt.q:
            q = q.where(
                or_(
                    JobRow.search.op("@@")(func.plainto_tsquery("simple", flt.q)),
                    JobRow.title.ilike(f"%{flt.q}%"),
                )
            )
        if flt.posted_within_days is not None:
            since = datetime.now(UTC) - timedelta(days=flt.posted_within_days)
            q = q.where(func.coalesce(JobRow.posted_at, JobRow.first_seen) >= since)
        q = q.order_by(func.coalesce(JobRow.posted_at, JobRow.first_seen).desc())
        q = q.limit(flt.limit).offset(flt.offset)
        async with self.session() as s:
            rows = (await s.scalars(q)).all()
        refs = await self._refs([r.id for r in rows])
        return [_to_job(r, refs.get(r.id, [])) for r in rows]

    async def set_status(self, job_id: str, status: JobStatus, reason: str) -> None:
        async with self.session.begin() as s:
            res = await s.execute(
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.status != status.value)
                .values(status=status.value)
            )
            if res.rowcount:  # type: ignore[attr-defined]
                s.add(JobStatusHistoryRow(job_id=job_id, status=status.value, reason=reason))

    async def _close(self, ids: list[str], reason: str) -> int:
        if not ids:
            return 0
        async with self.session.begin() as s:
            await s.execute(update(JobRow).where(JobRow.id.in_(ids)).values(status="closed"))
            s.add_all(JobStatusHistoryRow(job_id=i, status="closed", reason=reason) for i in ids)
        return len(ids)

    async def close_missing(
        self, source: str, company: str, seen_external_ids: set[str], *, reason: str
    ) -> int:
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(JobSourceRow.job_id, JobSourceRow.external_id)
                    .join(JobRow, JobRow.id == JobSourceRow.job_id)
                    .where(
                        JobSourceRow.source == source,
                        JobRow.status == "open",
                        JobRow.company_norm == normalize_company(company),
                    )
                )
            ).all()
        by_job: dict[str, list[str]] = {}
        for job_id, ext in rows:
            by_job.setdefault(job_id, []).append(ext)
        ids = [j for j, exts in by_job.items() if not set(exts) & seen_external_ids]
        return await self._close(ids, reason)

    async def close_stale(self, before: datetime) -> int:
        async with self.session() as s:
            ids = list(
                (
                    await s.scalars(
                        select(JobRow.id).where(JobRow.status == "open", JobRow.last_seen < before)
                    )
                ).all()
            )
        return await self._close(ids, "not seen by any crawl")

    async def record_run(self, run: CrawlRun) -> None:
        async with self.session.begin() as s:
            s.add(CrawlRunRow(**run.model_dump()))

    async def stats(self) -> Stats:
        async with self.session() as s:
            total = await s.scalar(
                select(func.count()).select_from(JobRow).where(JobRow.status == "open")
            )
            by_scope = (
                await s.execute(
                    select(JobRow.scope, func.count())
                    .where(JobRow.status == "open")
                    .group_by(JobRow.scope)
                )
            ).all()
            country = func.coalesce(JobRow.countries[1], "REMOTE/NA")
            by_country = (
                await s.execute(
                    select(country, func.count()).where(JobRow.status == "open").group_by(country)
                )
            ).all()
            by_source = (
                await s.execute(
                    select(JobSourceRow.source, func.count())
                    .join(JobRow, JobRow.id == JobSourceRow.job_id)
                    .where(JobRow.status == "open")
                    .group_by(JobSourceRow.source)
                )
            ).all()
            runs = (
                await s.scalars(select(CrawlRunRow).order_by(CrawlRunRow.id.desc()).limit(20))
            ).all()
        return Stats(
            total_open=total or 0,
            by_scope={k: v for k, v in by_scope},
            by_country={k: v for k, v in by_country},
            by_source={k: v for k, v in by_source},
            last_runs=[
                CrawlRun(
                    source_key=r.source_key,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    fetched=r.fetched,
                    new=r.new,
                    updated=r.updated,
                    error=r.error,
                )
                for r in runs
            ],
        )

    async def save_profile(self, profile: CandidateProfile) -> None:
        async with self.session.begin() as s:
            stmt = insert(ProfileRow).values(id=profile.id, data=profile.model_dump(mode="json"))
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[ProfileRow.id],
                    set_={"data": stmt.excluded.data, "updated_at": func.now()},
                )
            )

    async def get_profile(self, profile_id: str) -> CandidateProfile | None:
        async with self.session() as s:
            row = await s.get(ProfileRow, profile_id)
        return CandidateProfile.model_validate(row.data) if row else None

    async def save_scores(self, results: list[ScoreResult]) -> None:
        if not results:
            return
        async with self.session.begin() as s:
            for chunk_start in range(0, len(results), 500):
                chunk = results[chunk_start : chunk_start + 500]
                stmt = insert(ScoreRow).values(
                    [
                        {
                            "profile_id": r.profile_id,
                            "job_id": r.job_id,
                            "score": r.score,
                            "gated": bool(r.gates),
                            "result": r.model_dump(mode="json"),
                        }
                        for r in chunk
                    ]
                )
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[ScoreRow.profile_id, ScoreRow.job_id],
                        set_={
                            "score": stmt.excluded.score,
                            "gated": stmt.excluded.gated,
                            "result": stmt.excluded.result,
                            "scored_at": func.now(),
                        },
                    )
                )

    async def list_scores(
        self, profile_id: str, *, include_rejected: bool, limit: int
    ) -> list[tuple[Job, ScoreResult]]:
        q = (
            select(JobRow, ScoreRow)
            .join(ScoreRow, ScoreRow.job_id == JobRow.id)
            .where(ScoreRow.profile_id == profile_id)
        )
        if not include_rejected:
            q = q.where(ScoreRow.gated.is_(False))
        q = q.order_by(
            ScoreRow.score.desc(), func.coalesce(JobRow.posted_at, JobRow.first_seen).desc()
        ).limit(limit)
        async with self.session() as s:
            rows = (await s.execute(q)).all()
        refs = await self._refs([job.id for job, _ in rows])
        return [
            (_to_job(job, refs.get(job.id, [])), ScoreResult.model_validate(sc.result))
            for job, sc in rows
        ]

    async def upsert_application(self, app: Application) -> Application:
        async with self.session.begin() as s:
            stmt = insert(ApplicationRow).values(
                profile_id=app.profile_id,
                job_id=app.job_id,
                status=app.status.value,
                notes=app.notes,
            )
            row = (
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[ApplicationRow.profile_id, ApplicationRow.job_id],
                        set_={
                            "status": stmt.excluded.status,
                            "notes": stmt.excluded.notes,
                            "updated_at": func.now(),
                        },
                    ).returning(ApplicationRow.updated_at)
                )
            ).one()
        return app.model_copy(update={"updated_at": row[0]})

    async def list_applications(self, profile_id: str) -> list[Application]:
        async with self.session() as s:
            rows = (
                await s.scalars(
                    select(ApplicationRow)
                    .where(ApplicationRow.profile_id == profile_id)
                    .order_by(ApplicationRow.updated_at.desc())
                )
            ).all()
        return [
            Application(
                profile_id=r.profile_id,
                job_id=r.job_id,
                status=ApplicationStatus(r.status),
                notes=r.notes,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


async def create_schema(engine: AsyncEngine) -> None:
    """Test helper; production uses Alembic migrations."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
