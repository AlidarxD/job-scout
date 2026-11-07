from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jobscout.api.app import create_app
from jobscout.domain import Directness, RawPosting, RemotePolicy, Scope
from jobscout.extract.rules import RuleExtractor
from jobscout.pipeline import Ingestor
from jobscout.scoring.profile import CandidateProfile
from jobscout.storage.memory import InMemoryRepository


@pytest.fixture
def repo() -> InMemoryRepository:
    return InMemoryRepository()


@pytest.fixture
def client(repo: InMemoryRepository, settings) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(create_app(repo=repo, settings=settings)) as c:
        yield c


async def seed(repo: InMemoryRepository) -> None:
    ing = Ingestor(repo, RuleExtractor())
    await ing.ingest_posting(
        RawPosting(
            source="hh",
            external_id="1",
            url="https://hh.kz/vacancy/1",
            title="Senior Python Developer",
            company="Kolesa",
            location_raw="Алматы, Kazakhstan",
            description="Python, FastAPI, 3+ years",
            directness=Directness.BOARD,
        ),
        Scope.LOCAL,
    )
    await ing.ingest_posting(
        RawPosting(
            source="remotive",
            external_id="2",
            url="https://remotive.com/2",
            title="AI Engineer",
            company="Globex",
            location_raw="Worldwide",
            remote_hint=RemotePolicy.REMOTE,
            description="LLM, RAG, Python",
        ),
        Scope.GLOBAL,
    )
    await ing.ingest_posting(
        RawPosting(
            source="greenhouse",
            external_id="3",
            url="https://gh/3",
            title="ML Engineer",
            company="Tokyo AI",
            location_raw="Tokyo, Japan",
            description="Japanese (N1) required.",
            directness=Directness.EMPLOYER_ATS,
        ),
        Scope.GLOBAL,
    )


async def test_job_board_filters(client: TestClient, repo: InMemoryRepository) -> None:
    await seed(repo)
    assert client.get("/health").json() == {"status": "ok"}
    assert len(client.get("/api/jobs").json()) == 3
    local = client.get("/api/jobs", params={"scope": "local"}).json()
    assert [j["company"] for j in local] == ["Kolesa"]
    # "Open to KZ" = office in KZ or remote open to KZ
    open_kz = {j["company"] for j in client.get("/api/jobs", params={"country": "KZ"}).json()}
    assert open_kz == {"Kolesa", "Globex"}
    remote = client.get("/api/jobs", params={"remote": "remote"}).json()
    assert remote[0]["remote_regions"] == ["WORLD"]
    assert client.get("/api/jobs", params={"seniority": "wizard"}).status_code == 422
    detail = client.get(f"/api/jobs/{local[0]['id']}").json()
    assert "FastAPI" in detail["description"]
    assert client.get("/api/jobs/nope").status_code == 404
    stats = client.get("/api/stats").json()
    assert stats["total_open"] == 3 and stats["by_scope"] == {"local": 1, "global": 2}
    assert client.get("/").status_code == 200


async def test_profile_matches_and_applications(
    client: TestClient, repo: InMemoryRepository, profile: CandidateProfile
) -> None:
    await seed(repo)
    body = profile.model_dump(mode="json")
    assert client.put("/api/profiles/me", json=body).status_code == 200
    summary = client.post("/api/profiles/me/score").json()
    assert summary["scored"] == 3 and summary["rejected"] >= 1
    matches = client.get("/api/profiles/me/matches").json()
    assert "Tokyo AI" not in {m["job"]["company"] for m in matches}
    with_rejected = client.get("/api/profiles/me/matches", params={"include_rejected": True}).json()
    tokyo = next(m for m in with_rejected if m["job"]["company"] == "Tokyo AI")
    assert tokyo["score"]["gates"][0]["code"] == "language"

    job_id = matches[0]["job"]["id"]
    app = client.put(
        f"/api/profiles/me/applications/{job_id}",
        json={"status": "applied", "notes": "referral from Aigerim"},
    )
    assert app.status_code == 200 and app.json()["status"] == "applied"
    assert client.get("/api/profiles/me/applications").json()[0]["job_id"] == job_id
    assert (
        client.put("/api/profiles/me/applications/nope", json={"status": "applied"}).status_code
        == 404
    )
