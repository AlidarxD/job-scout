from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobscout.domain import (
    Directness,
    Job,
    JobFacts,
    JobStatus,
    RawPosting,
    RemotePolicy,
    Salary,
    Scope,
    Seniority,
)
from jobscout.extract.rules import RuleExtractor
from jobscout.normalize.dedup import fingerprint, normalize_company, normalize_title
from jobscout.pipeline import Ingestor, score_all
from jobscout.scoring.profile import CandidateProfile
from jobscout.scoring.rubric import score
from jobscout.sources.base import Source
from jobscout.storage.memory import InMemoryRepository


def job(**facts: object) -> Job:
    title = str(facts.pop("title", "Senior AI Engineer"))
    return Job(id="j1", title=title, company="Acme", url="https://x", facts=JobFacts(**facts))


# --- dedup ---------------------------------------------------------------------------------


def test_normalizers() -> None:
    assert normalize_company("ТОО Kolesa Group") == normalize_company("Kolesa Group LLP")
    assert normalize_company("Stripe, Inc.") == "stripe"
    assert normalize_title("Senior Backend Engineer (Remote)") == "senior backend engineer"
    assert normalize_title("Sr. Backend Engineer - Berlin") == "senior backend engineer"
    assert fingerprint("Stripe, Inc.", "Sr. Backend Engineer", "US") == fingerprint(
        "stripe", "Senior Backend Engineer (Remote)", "US"
    )


# --- gates and rubric ------------------------------------------------------------------------


def test_remote_worldwide_strong_match(profile: CandidateProfile) -> None:
    res = score(
        job(
            seniority=Seniority.SENIOR,
            remote=RemotePolicy.REMOTE,
            remote_regions=["WORLD"],
            skills=["python", "llm", "rag", "fastapi"],
        ),
        profile,
    )
    assert not res.gates
    assert res.passed and res.score >= 8
    assert res.why_it_works == "remote worldwide"


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        ({"citizenship_required": ["SA"], "countries": ["SA"]}, "citizenship"),
        ({"required_languages": ["ja"], "countries": ["JP"]}, "language"),
        ({"remote": RemotePolicy.REMOTE, "remote_regions": ["US"]}, "remote_region"),
        ({"countries": ["BR"]}, "geography"),
        ({"countries": ["DE"], "visa_sponsorship": False}, "visa"),
        ({"seniority": Seniority.INTERN}, "too_junior"),
        ({"seniority": Seniority.PRINCIPAL}, "too_senior"),
        ({"salary": Salary(min=200_000, max=300_000, currency="KZT", period="month")}, "salary"),
    ],
)
def test_gates_fire_with_reason(
    profile: CandidateProfile, facts: dict[str, object], code: str
) -> None:
    res = score(job(**facts), profile)
    assert code in {g.code for g in res.gates}, res.gates
    assert not res.passed
    assert all(g.reason for g in res.gates)


def test_kz_language_is_fine_for_kz_candidate(profile: CandidateProfile) -> None:
    res = score(job(required_languages=["kk", "ru"], countries=["KZ"]), profile)
    assert not res.gates
    assert res.why_it_works == "office in KZ"


def test_closed_and_blacklisted(profile: CandidateProfile) -> None:
    closed = job().model_copy(update={"status": JobStatus.CLOSED})
    assert {g.code for g in score(closed, profile).gates} == {"closed"}
    black = profile.model_copy(update={"blacklist": ["ACME Inc."]})
    assert {g.code for g in score(job(), black).gates} == {"blacklist"}


def test_unknowns_are_neutral_not_hopeful(profile: CandidateProfile) -> None:
    res = score(job(title="Software Engineer"), profile)
    assert res.dimensions["skills"] == 5.0
    assert res.dimensions["seniority"] == 5.0
    assert res.score < profile.fit_threshold


def test_relocation_needs_stated_sponsorship_for_high_location_score(
    profile: CandidateProfile,
) -> None:
    stated = score(job(countries=["DE"], visa_sponsorship=True, relocation=True), profile)
    silent = score(job(countries=["DE"]), profile)
    assert stated.dimensions["location"] == 9.0
    assert silent.dimensions["location"] == 5.0


# --- ingest pipeline ---------------------------------------------------------------------------


class FakeSource(Source):
    kind = "greenhouse"
    directness = Directness.EMPLOYER_ATS

    def __init__(self, postings: list[RawPosting], settings, **opts):  # type: ignore[no-untyped-def]
        super().__init__(settings, **opts)
        self.postings = postings

    @property
    def key(self) -> str:
        return f"fake:{self.options.get('board', 'x')}"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        for p in self.postings:
            yield p


class BoomSource(FakeSource):
    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        raise RuntimeError("board down")
        yield  # pragma: no cover


def raw(source: str, ext: str, directness: Directness, **kw: object) -> RawPosting:
    base: dict[str, object] = dict(
        source=source,
        external_id=ext,
        url=f"https://{source}.example/{ext}",
        title="Senior Backend Engineer",
        company="Acme",
        location_raw="Berlin, Germany",
        description="Python, Kafka. 5+ years.",
        directness=directness,
        posted_at=datetime.now(UTC) - timedelta(days=2),
    )
    base.update(kw)
    return RawPosting(**base)  # type: ignore[arg-type]


async def test_cross_source_dedup_prefers_employer_link(settings) -> None:  # type: ignore[no-untyped-def]
    repo = InMemoryRepository()
    ing = Ingestor(repo, RuleExtractor())
    linkedin = raw("linkedin", "li-1", Directness.AGGREGATOR, title="Sr. Backend Engineer")
    ats = raw(
        "greenhouse",
        "gh-1",
        Directness.EMPLOYER_ATS,
        description="Python, Kafka. 5+ years. We sponsor visas. Full description here.",
    )
    assert await ing.ingest_posting(linkedin, Scope.GLOBAL) == "new"
    assert await ing.ingest_posting(ats, Scope.GLOBAL) == "updated"
    assert len(repo.jobs) == 1
    (stored,) = repo.jobs.values()
    assert stored.url == "https://greenhouse.example/gh-1"
    assert {s.source for s in stored.sources} == {"linkedin", "greenhouse"}
    assert stored.facts.visa_sponsorship is True  # re-extracted from the canonical text
    assert stored.facts.countries == ["DE"]


async def test_kz_jobs_are_local_scope(settings) -> None:  # type: ignore[no-untyped-def]
    repo = InMemoryRepository()
    await Ingestor(repo, RuleExtractor()).ingest_posting(
        raw("workable", "w1", Directness.EMPLOYER_ATS, location_raw="Almaty, Kazakhstan"),
        Scope.GLOBAL,
    )
    (stored,) = repo.jobs.values()
    assert stored.scope is Scope.LOCAL


async def test_run_closes_removed_ats_postings_and_survives_failures(settings) -> None:  # type: ignore[no-untyped-def]
    repo = InMemoryRepository()
    ing = Ingestor(repo, RuleExtractor())
    a = raw("greenhouse", "1", Directness.EMPLOYER_ATS, title="Backend Engineer")
    b = raw("greenhouse", "2", Directness.EMPLOYER_ATS, title="ML Engineer")
    async with httpx.AsyncClient() as client:
        await ing.run([FakeSource([a, b], settings, board="acme", company="Acme")], client)
        assert sum(j.status is JobStatus.OPEN for j in repo.jobs.values()) == 2
        report = await ing.run(
            [
                FakeSource([a], settings, board="acme", company="Acme"),
                BoomSource([], settings, board="down", company="Down"),
            ],
            client,
        )
    statuses = {j.title: j.status for j in repo.jobs.values()}
    assert statuses == {"Backend Engineer": JobStatus.OPEN, "ML Engineer": JobStatus.CLOSED}
    errors = [r for r in report.runs if r.error]
    assert len(errors) == 1 and "board down" in (errors[0].error or "")


async def test_score_all_and_rejected_list(profile: CandidateProfile, settings) -> None:  # type: ignore[no-untyped-def]
    repo = InMemoryRepository()
    ing = Ingestor(repo, RuleExtractor())
    await ing.ingest_posting(
        raw(
            "remotive",
            "r1",
            Directness.AGGREGATOR,
            title="Senior AI Engineer",
            location_raw="Worldwide",
            remote_hint=RemotePolicy.REMOTE,
            description="LLM, RAG, Python, FastAPI. Work from anywhere.",
        ),
        Scope.GLOBAL,
    )
    await ing.ingest_posting(
        raw(
            "greenhouse",
            "g1",
            Directness.EMPLOYER_ATS,
            title="Data Analyst",
            location_raw="Riyadh, Saudi Arabia",
            description="Saudi nationals only.",
        ),
        Scope.GLOBAL,
    )
    results = await score_all(repo, profile)
    assert len(results) == 2
    shortlist = await repo.list_scores(profile.id, include_rejected=False, limit=10)
    assert [j.title for j, _ in shortlist] == ["Senior AI Engineer"]
    everything = await repo.list_scores(profile.id, include_rejected=True, limit=10)
    rejected = [r for _, r in everything if r.gates]
    assert rejected and rejected[0].gates[0].code == "citizenship"
