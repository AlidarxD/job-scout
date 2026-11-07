from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from jobscout.domain import Directness, RemotePolicy
from jobscout.sources import REGISTRY, load_sources
from jobscout.sources.ats import (
    GreenhouseSource,
    parse_ashby,
    parse_greenhouse,
    parse_lever,
    parse_smartrecruiters,
    parse_workable,
)
from jobscout.sources.hn import parse_hn_thread
from jobscout.sources.kz import (
    HeadHunterSource,
    parse_hh_item,
    parse_html_board,
    parse_telegram_page,
)
from jobscout.sources.linkedin import parse_linkedin_items
from jobscout.sources.remote_boards import (
    parse_arbeitnow,
    parse_himalayas,
    parse_money_range,
    parse_remoteok,
    parse_remotive,
)
from tests.conftest import load


def test_greenhouse_unescapes_html_and_keeps_direct_link() -> None:
    jobs = parse_greenhouse(load("greenhouse.json"), "Acme")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Software Engineer, LLM Infrastructure"
    assert first.company == "Acme AI"
    assert "5+ years of backend experience" in first.description
    assert "<" not in first.description
    assert first.remote_hint is RemotePolicy.REMOTE
    assert first.directness is Directness.EMPLOYER_ATS
    assert first.posted_at == datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def test_lever_salary_lists_and_workplace() -> None:
    (job,) = parse_lever(load("lever.json"), "Acme")
    assert job.remote_hint is RemotePolicy.HYBRID
    assert job.country_hint == "JP"
    assert job.salary is not None and job.salary.currency == "JPY" and job.salary.period == "year"
    assert "JLPT N2" in job.description and "Relocation support" in job.description


def test_ashby_skips_unlisted_and_reads_compensation() -> None:
    jobs = parse_ashby(load("ashby.json"), "Acme")
    assert [j.external_id for j in jobs] == ["f00d-1"]
    job = jobs[0]
    assert job.remote_hint is RemotePolicy.REMOTE
    assert job.salary is not None and (job.salary.min, job.salary.max) == (180000, 240000)
    assert job.hints["countries"] == ["US"]
    assert job.location_raw == "Remote (US) / New York, NY"


def test_workable_country_codes() -> None:
    (job,) = parse_workable(load("workable.json"), "x")
    assert job.company == "Kaspi Labs"
    assert job.hints["countries"] == ["KZ"]
    assert job.location_raw == "Almaty, Kazakhstan"


def test_smartrecruiters_merges_detail() -> None:
    detail = load("smartrecruiters_detail.json")
    (job,) = parse_smartrecruiters(
        load("smartrecruiters_list.json"), "AcmeGroup", "Acme", {detail["id"]: detail}
    )
    assert job.url.endswith("7440001-backend-engineer-java")
    assert "relocation package" in job.description
    assert job.hints["countries"] == ["PL"]
    assert job.hints["seniority"] == "Mid-Senior Level"


def test_hh_list_item_with_detail() -> None:
    items = load("hh_list.json")["items"]
    job = parse_hh_item(items[0], load("hh_detail.json"))
    assert job.url == "https://hh.kz/vacancy/110000001"
    assert job.company == "ТОО Kolesa Group"
    assert job.salary is not None
    assert (job.salary.min, job.salary.max, job.salary.currency, job.salary.period) == (
        1_200_000,
        1_800_000,
        "KZT",
        "month",
    )
    assert job.remote_hint is RemotePolicy.HYBRID
    assert job.hints["years_min"] == 3
    assert job.hints["skills"] == ["Python", "FastAPI", "PostgreSQL"]
    assert "Kubernetes" in job.description
    assert job.posted_at == datetime(2026, 11, 5, 5, 30, tzinfo=UTC)


def test_hh_list_item_without_detail_uses_snippet() -> None:
    job = parse_hh_item(load("hh_list.json")["items"][1])
    assert job.remote_hint is RemotePolicy.REMOTE
    assert "казахского" in job.description
    assert "<highlighttext>" not in job.description
    assert job.location_raw == "Астана, Kazakhstan"


@respx.mock
async def test_hh_fetch_sends_user_agent_and_paginates(settings) -> None:  # type: ignore[no-untyped-def]
    list_route = respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json=load("hh_list.json"))
    )
    respx.get("https://api.hh.ru/vacancies/110000001").mock(
        return_value=httpx.Response(200, json=load("hh_detail.json"))
    )
    respx.get("https://api.hh.ru/vacancies/110000002").mock(return_value=httpx.Response(404))
    source = HeadHunterSource(settings, text="python")
    async with httpx.AsyncClient() as client:
        postings = [p async for p in source.fetch(client)]
    assert len(postings) == 2
    request = list_route.calls[0].request
    assert request.headers["HH-User-Agent"] == settings.user_agent
    assert request.url.params["area"] == "40" and request.url.params["host"] == "hh.kz"
    assert postings[0].hints.get("skills")  # detail merged
    assert "skills" not in postings[1].hints  # 404 on detail falls back to snippet


@respx.mock
async def test_greenhouse_fetch_retries_on_429(settings) -> None:  # type: ignore[no-untyped-def]
    route = respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=load("greenhouse.json")),
        ]
    )
    async with httpx.AsyncClient() as client:
        postings = [p async for p in GreenhouseSource(settings, board="acme").fetch(client)]
    assert route.call_count == 2
    assert len(postings) == 2


def test_telegram_page_filters_non_vacancies() -> None:
    postings, oldest = parse_telegram_page(load("telegram.html"), "kz_it_jobs")
    assert oldest == 501
    assert len(postings) == 1
    job = postings[0]
    assert job.title == "Middle Python Developer"
    assert job.company == "Chocofamily"
    assert job.location_raw == "Алматы, гибрид"
    assert job.apply_url == "https://forms.example/apply"
    assert job.salary is not None and (job.salary.min, job.salary.max) == (800_000, 1_100_000)
    assert job.salary.currency == "KZT" and job.salary.period == "month"
    assert job.url == "https://t.me/kz_it_jobs/501"


def test_html_board_with_selectors() -> None:
    opts = {
        "name": "dsml",
        "url": "https://dsml.kz/jobs",
        "base_url": "https://dsml.kz",
        "item": "article.job",
        "title": ".title",
        "link": "a[href]",
        "company": ".company",
        "location": ".location",
        "date": "time",
        "country": "KZ",
    }
    jobs = parse_html_board(load("html_board.html"), opts)
    assert [j.url for j in jobs] == [
        "https://dsml.kz/jobs/ml-engineer-12",
        "https://dsml.kz/jobs/da-13",
    ]
    assert jobs[0].company == "Beeline KZ" and jobs[0].country_hint == "KZ"
    assert jobs[0].posted_at is not None


def test_remote_boards() -> None:
    (rem,) = parse_remotive(load("remotive.json"))
    assert rem.hints["remote_regions"] == ["WORLD"]
    assert rem.salary is not None and (rem.salary.min, rem.salary.max) == (90000, 120000)

    (rok,) = parse_remoteok(load("remoteok.json"))  # legal notice row skipped
    assert rok.hints["remote_regions"] == ["EU"]

    (arb,) = parse_arbeitnow(load("arbeitnow.json"))
    assert arb.remote_hint is RemotePolicy.UNKNOWN and arb.location_raw == "Berlin"

    (him,) = parse_himalayas(load("himalayas.json"))
    assert him.hints["remote_regions"] == ["US", "CA"]
    assert him.hints["seniority"] == "Mid-level"


def test_hn_parses_pipe_header_and_skips_noise() -> None:
    (job,) = parse_hn_thread(load("hn_item.json"))
    assert job.company == "Pied Piper"
    assert job.title == "Senior Backend Engineer"
    assert job.location_raw == "Remote (Worldwide)"
    assert job.remote_hint is RemotePolicy.REMOTE
    assert job.apply_url == "https://piedpiper.example/jobs"
    assert job.salary is not None and job.salary.max == 190000


def test_linkedin_tolerant_mapping() -> None:
    (job,) = parse_linkedin_items(load("linkedin.json"))
    assert job.url == "https://www.linkedin.com/jobs/view/4100000001/"
    assert job.external_id == "4100000001"
    assert job.hints["seniority"] == "Mid-Senior level"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120k - $150k", (120000, 150000, "USD", "year")),
        ("€60,000–80,000", (60000, 80000, "EUR", "year")),
        ("5000 USD / month", (5000, 5000, "USD", "month")),
        ("competitive", None),
    ],
)
def test_parse_money_range(text, expected) -> None:  # type: ignore[no-untyped-def]
    sal = parse_money_range(text)
    if expected is None:
        assert sal is None
    else:
        assert sal is not None
        assert (sal.min, sal.max, sal.currency, sal.period) == expected


def test_config_loads_every_kind(settings) -> None:  # type: ignore[no-untyped-def]
    sources = load_sources(settings)
    kinds = {s.kind for s in sources}
    assert {"hh", "greenhouse", "ashby", "lever", "remotive", "hn", "linkedin"} <= kinds
    assert all(s.kind in REGISTRY for s in sources)
    assert len({s.key for s in sources}) == len(sources), "source keys must be unique"
