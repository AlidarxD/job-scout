"""Postgres repository against a real database (CI service container or local Postgres).

Run with: JOBSCOUT_TEST_DATABASE_URL=postgresql+asyncpg://... pytest -m integration
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from jobscout.domain import Directness, JobStatus, RawPosting, RemotePolicy, Scope
from jobscout.extract.rules import RuleExtractor
from jobscout.pipeline import Ingestor, score_all
from jobscout.scoring.profile import CandidateProfile
from jobscout.storage.base import Application, ApplicationStatus, JobFilter
from jobscout.storage.sql import Base, SqlRepository, create_schema

URL = os.environ.get("JOBSCOUT_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="JOBSCOUT_TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def repo() -> AsyncIterator[SqlRepository]:
    assert URL
    r = SqlRepository.from_url(URL)
    async with r.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_schema(r.engine)
    yield r
    await r.close()


def posting(ext: str, **kw: object) -> RawPosting:
    base: dict[str, object] = dict(
        source="greenhouse",
        external_id=ext,
        url=f"https://gh/{ext}",
        title=f"Engineer {ext}",
        company="Acme Inc",
        location_raw="Berlin, Germany",
        description="Python, Kafka",
        directness=Directness.EMPLOYER_ATS,
        posted_at=datetime.now(UTC) - timedelta(days=1),
    )
    base.update(kw)
    return RawPosting(**base)  # type: ignore[arg-type]


async def test_roundtrip_dedup_filters_and_close(repo: SqlRepository) -> None:
    ing = Ingestor(repo, RuleExtractor())
    await ing.ingest_posting(posting("1"), Scope.GLOBAL)
    await ing.ingest_posting(
        posting(
            "li-1",
            source="linkedin",
            directness=Directness.AGGREGATOR,
            url="https://linkedin/1",
            title="Engineer 1",
        ),
        Scope.GLOBAL,
    )
    await ing.ingest_posting(
        posting(
            "2",
            title="AI Engineer",
            location_raw="Worldwide",
            remote_hint=RemotePolicy.REMOTE,
            description="LLM, RAG",
        ),
        Scope.GLOBAL,
    )
    await ing.ingest_posting(
        posting(
            "3",
            source="hh",
            title="Python разработчик",
            location_raw="Алматы, Kazakhstan",
            directness=Directness.BOARD,
        ),
        Scope.LOCAL,
    )

    jobs = await repo.list_jobs(JobFilter())
    assert len(jobs) == 3
    merged = next(j for j in jobs if j.title == "Engineer 1")
    assert {s.source for s in merged.sources} == {"greenhouse", "linkedin"}
    assert merged.url == "https://gh/1"

    assert [j.title for j in await repo.list_jobs(JobFilter(scope=Scope.LOCAL))] == [
        "Python разработчик"
    ]
    open_to_kz = {j.title for j in await repo.list_jobs(JobFilter(country="KZ"))}
    assert open_to_kz == {"Python разработчик", "AI Engineer"}
    assert [j.title for j in await repo.list_jobs(JobFilter(q="python"))] == ["Python разработчик"]
    assert len(await repo.list_jobs(JobFilter(source="linkedin"))) == 1

    closed = await repo.close_missing("greenhouse", "Acme", {"2"}, reason="removed")
    assert closed == 1  # Engineer 1 gone from the board; hh job untouched (other source)
    gone = await repo.get_job(merged.id)
    assert gone is not None and gone.status is JobStatus.CLOSED
    async with repo.engine.connect() as conn:
        reasons = (
            (
                await conn.execute(
                    text("select reason from job_status_history where job_id=:j order by id"),
                    {"j": merged.id},
                )
            )
            .scalars()
            .all()
        )
    assert reasons == ["first seen", "removed"]

    stats = await repo.stats()
    assert stats.total_open == 2 and stats.by_scope == {"global": 1, "local": 1}


async def test_scores_and_applications(repo: SqlRepository, profile: CandidateProfile) -> None:
    ing = Ingestor(repo, RuleExtractor())
    await ing.ingest_posting(
        posting(
            "10",
            title="Senior AI Engineer",
            location_raw="Worldwide",
            remote_hint=RemotePolicy.REMOTE,
            description="LLM, RAG, Python, FastAPI",
        ),
        Scope.GLOBAL,
    )
    await ing.ingest_posting(
        posting(
            "11",
            title="Analyst",
            location_raw="Riyadh, Saudi Arabia",
            description="Saudi nationals only",
        ),
        Scope.GLOBAL,
    )
    results = await score_all(repo, profile)
    assert len(results) == 2
    assert (await repo.get_profile(profile.id)) == profile
    top = await repo.list_scores(profile.id, include_rejected=False, limit=5)
    assert [j.title for j, _ in top] == ["Senior AI Engineer"]
    await score_all(repo, profile)  # idempotent upsert
    assert len(await repo.list_scores(profile.id, include_rejected=True, limit=5)) == 2

    job_id = top[0][0].id
    app = await repo.upsert_application(
        Application(profile_id=profile.id, job_id=job_id, status=ApplicationStatus.APPLIED)
    )
    assert app.updated_at is not None
    await repo.upsert_application(
        Application(
            profile_id=profile.id,
            job_id=job_id,
            status=ApplicationStatus.INTERVIEWING,
            notes="call",
        )
    )
    (stored,) = await repo.list_applications(profile.id)
    assert stored.status is ApplicationStatus.INTERVIEWING and stored.notes == "call"
