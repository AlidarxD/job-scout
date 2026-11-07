from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jobscout.domain import RawPosting, RemotePolicy, Seniority
from jobscout.evals import run_eval
from jobscout.extract.llm import LlmExtractor
from jobscout.extract.rules import RuleExtractor, parse_salary_text, required_languages, years_min
from jobscout.normalize.geo import country_in_regions, detect_countries, detect_regions
from jobscout.sources.ats import parse_greenhouse, parse_lever
from tests.conftest import load


def posting(**kw: Any) -> RawPosting:
    base = {
        "source": "test",
        "external_id": "1",
        "url": "https://x/1",
        "title": "Engineer",
        "company": "Acme",
    }
    return RawPosting(**{**base, **kw})


rules = RuleExtractor()


def test_senior_in_disguise() -> None:
    facts = rules.extract_sync(
        posting(title="Backend Engineer", description="Requires 6+ years of experience with Go.")
    )
    assert facts.years_min == 6
    assert facts.seniority is Seniority.SENIOR


def test_title_level_wins_over_years_for_lead() -> None:
    facts = rules.extract_sync(posting(title="Tech Lead, Payments", description="3+ years"))
    assert facts.seniority is Seniority.LEAD


def test_saudization_is_citizenship_gate() -> None:
    (_, analyst) = parse_greenhouse(load("greenhouse.json"), "Acme")
    facts = rules.extract_sync(analyst)
    assert facts.citizenship_required == ["SA"]
    assert facts.countries == ["SA"]


def test_visa_yes_and_no() -> None:
    assert rules.extract_sync(posting(description="We can sponsor visas.")).visa_sponsorship is True
    assert (
        rules.extract_sync(
            posting(description="We are unable to sponsor work visas for this role.")
        ).visa_sponsorship
        is False
    )
    assert rules.extract_sync(posting(description="Great team.")).visa_sponsorship is None


def test_lever_japanese_required_and_relocation() -> None:
    (job,) = parse_lever(load("lever.json"), "Acme")
    facts = rules.extract_sync(job)
    assert facts.required_languages == ["ja"]
    assert facts.relocation is True
    assert facts.visa_sponsorship is True
    assert facts.countries == ["JP"]
    assert facts.remote is RemotePolicy.HYBRID


def test_language_nice_to_have_is_not_required() -> None:
    assert required_languages("Polish is a plus.") == []
    assert required_languages("Fluent German required") == ["de"]
    assert required_languages("Знание казахского языка обязательно") == ["kk"]


def test_country_locked_remote() -> None:
    facts = rules.extract_sync(
        posting(
            location_raw="Remote",
            remote_hint=RemotePolicy.REMOTE,
            description="Must be authorized to work in the United States.",
        )
    )
    assert facts.remote_regions == ["US"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Зарплата: 800 000 – 1 100 000 ₸", (800_000, 1_100_000, "KZT", "month")),
        ("от 500 000 тг на руки", (500_000, None, "KZT", "month")),
        ("$120k–$150k base", (120_000, 150_000, "USD", "year")),
        ("€4,500 per month", (4_500, 4_500, "EUR", "month")),
        ("5 years of experience", None),
    ],
)
def test_salary_text(text: str, expected: tuple[Any, ...] | None) -> None:
    sal = parse_salary_text(text)
    if expected is None:
        assert sal is None
    else:
        assert sal is not None
        assert (sal.min, sal.max, sal.currency, sal.period) == expected


def test_years_russian() -> None:
    assert years_min("Опыт коммерческой разработки на Python от 3 лет.") == 3
    assert years_min("опыт работы не менее 5 лет") == 5


def test_geo() -> None:
    assert detect_countries("Almaty, Kazakhstan") == ["KZ"]
    assert detect_countries("Remote - US or Canada") == ["US", "CA"]
    assert detect_countries("Focus on business") == []  # no "us" false positive
    assert detect_regions("Anywhere in the world") == ["WORLD"]
    assert country_in_regions("KZ", ["EMEA"])
    assert not country_in_regions("KZ", ["US"])
    assert country_in_regions("PL", ["EU"])


async def test_rule_baseline_eval_set() -> None:
    report = await run_eval(rules)
    assert report.n >= 20
    assert report.micro_accuracy >= 0.8, report.table()


# --- LLM extractor with a fake client ------------------------------------------------------


class FakeMessages:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        data = self.outputs.pop(0)
        block = SimpleNamespace(type="tool_use", id=f"tu_{len(self.calls)}", input=data)
        return SimpleNamespace(
            content=[block], usage=SimpleNamespace(input_tokens=1000, output_tokens=100)
        )


def fake_client(*outputs: dict[str, Any]) -> Any:
    return SimpleNamespace(messages=FakeMessages(list(outputs)))


GOOD = {
    "seniority": "senior",
    "years_min": 5,
    "remote": "remote",
    "remote_regions": ["emea"],
    "countries": [],
    "visa_sponsorship": True,
    "relocation": None,
    "citizenship_required": [],
    "required_languages": [],
    "skills": ["Python", "Kafka"],
    "salary": None,
}


async def test_llm_extractor_forces_tool_and_applies_hints() -> None:
    client = fake_client(GOOD)
    ext = LlmExtractor(client, "claude-haiku-5-5")
    facts = await ext.extract(
        posting(hints={"skills": ["FastAPI"]}, remote_hint=RemotePolicy.HYBRID)
    )
    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_facts"}
    assert facts.seniority is Seniority.SENIOR
    assert facts.remote_regions == ["EMEA"]
    assert facts.skills[:1] == ["fastapi"]  # source hint first
    assert facts.remote is RemotePolicy.HYBRID  # structured source data beats the model
    assert ext.usage.input_tokens == 1000


async def test_llm_extractor_repairs_then_succeeds() -> None:
    bad = {**GOOD, "seniority": "rockstar"}
    client = fake_client(bad, GOOD)
    ext = LlmExtractor(client, "m")
    facts = await ext.extract(posting())
    assert facts.seniority is Seniority.SENIOR
    second = client.messages.calls[1]["messages"]
    assert second[-1]["content"][0]["is_error"] is True


async def test_llm_extractor_falls_back_to_rules() -> None:
    bad = {**GOOD, "remote": "sometimes"}
    ext = LlmExtractor(fake_client(bad, bad), "m")
    facts = await ext.extract(posting(title="Senior Engineer"))
    assert facts.seniority is Seniority.SENIOR  # from rules
    assert ext.usage.fallbacks == 1


def test_skill_regex_alternatives_respect_word_boundaries() -> None:
    from jobscout.extract.rules import skills

    found = skills("Опыт от 2 лет, Django, PostgreSQL. Mongo is a plus.")
    assert "go" not in found
    assert {"django", "postgresql", "mongodb"} <= set(found)
    assert "go" in skills("Backend in Go, Kafka")


def test_visa_sponsorship_and_relocation_phrase() -> None:
    facts = rules.extract_sync(
        posting(description="Visa sponsorship and relocation package provided.")
    )
    assert facts.visa_sponsorship is True and facts.relocation is True
