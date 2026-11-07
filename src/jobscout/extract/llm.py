"""LLM field extraction with schema-forced tool use and Pydantic validation.

Design:
- The model must call a single tool whose input schema *is* the Pydantic schema, so the
  output is structurally valid JSON by construction; Pydantic then enforces enums/ranges.
- One repair round-trip on validation error, then fall back to the rule extractor, so a bad
  generation never blocks ingestion.
- Structured hints from the source (hh experience, key skills) override the model.
- Token usage is accumulated so cost per 1k postings is visible in eval and run logs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from jobscout.domain import JobFacts, RawPosting, RemotePolicy, Salary, Seniority
from jobscout.extract.rules import RuleExtractor

log = logging.getLogger(__name__)

PROMPT_VERSION = "facts-v1"
MAX_DESCRIPTION_CHARS = 12_000

SYSTEM = """You extract hiring facts from a job posting. Be literal and conservative:
- Only report what the posting states or unambiguously implies. Unknown -> null / [] / "unknown".
- seniority: the level the role is actually hired at. If the title says Mid but it demands
  5+ years or owning architecture, answer senior. Staff / tech lead / team lead -> lead;
  principal / distinguished / director -> principal.
- remote_regions: where a remote hire may live. "Remote (US only)" -> ["US"]; "Remote, EMEA"
  -> ["EMEA"]; "Anywhere" -> ["WORLD"]. Allowed tokens: ISO2 codes or WORLD, EU, EMEA,
  AMERICAS, APAC, CIS (North/Latin America -> AMERICAS). [] when not remote or not stated.
- countries: ISO-3166 alpha-2 codes of office locations.
- citizenship_required: ISO2 codes when the role is restricted to nationals or needs a
  clearance (Saudization -> SA, Emiratisation -> AE, US security clearance -> US).
  Only hard requirements: "nationals preferred" is NOT a requirement -> [].
- required_languages: ISO-639-1 codes of human languages *required* besides English. Ignore
  "nice to have".
- visa_sponsorship / relocation: true only if offered, false only if explicitly refused.
- salary: only if a number is printed. period is year|month|hour.
Call the record_facts tool exactly once."""


class LlmSalary(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217")
    period: Literal["year", "month", "hour"] = "year"


class LlmFacts(BaseModel):
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "principal", "unknown"]
    years_min: int | None = Field(default=None, ge=0, le=20)
    remote: Literal["onsite", "hybrid", "remote", "unknown"]
    remote_regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    visa_sponsorship: bool | None = None
    relocation: bool | None = None
    citizenship_required: list[str] = Field(default_factory=list)
    required_languages: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, max_length=40)
    salary: LlmSalary | None = None

    def to_facts(self) -> JobFacts:
        return JobFacts(
            seniority=Seniority[self.seniority.upper()],
            years_min=self.years_min,
            remote=RemotePolicy(self.remote),
            remote_regions=normalize_regions(self.remote_regions),
            countries=[c.upper() for c in self.countries if len(c) == 2],
            visa_sponsorship=self.visa_sponsorship,
            relocation=self.relocation,
            citizenship_required=[c.upper() for c in self.citizenship_required if len(c) == 2],
            required_languages=[lang.lower() for lang in self.required_languages],
            skills=[s.lower() for s in self.skills],
            salary=Salary(**self.salary.model_dump()) if self.salary else None,
        )


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallbacks: int = 0
    errors: list[str] = field(default_factory=list)


class LlmExtractor:
    """Anthropic-backed extractor. `client` is an `anthropic.AsyncAnthropic` (or a fake)."""

    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model
        self.name = f"llm:{model}:{PROMPT_VERSION}"
        self.usage = Usage()
        self._fallback = RuleExtractor()

    @classmethod
    def from_settings(cls, api_key: str, model: str) -> LlmExtractor:
        import anthropic

        return cls(anthropic.AsyncAnthropic(api_key=api_key), model)

    def _user_message(self, posting: RawPosting) -> str:
        return (
            f"Title: {posting.title}\nCompany: {posting.company}\n"
            f"Location: {posting.location_raw or 'n/a'}\nSource: {posting.source}\n\n"
            f"{posting.description[:MAX_DESCRIPTION_CHARS]}"
        )

    async def _call(self, messages: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM,
            tools=[
                {
                    "name": "record_facts",
                    "description": "Record the extracted facts.",
                    "input_schema": LlmFacts.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": "record_facts"},
            messages=messages,
        )
        self.usage.calls += 1
        self.usage.input_tokens += getattr(resp.usage, "input_tokens", 0)
        self.usage.output_tokens += getattr(resp.usage, "output_tokens", 0)
        block = next(b for b in resp.content if getattr(b, "type", None) == "tool_use")
        return dict(block.input), block.id

    async def extract(self, posting: RawPosting) -> JobFacts:
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._user_message(posting)}]
        try:
            data, tool_id = await self._call(messages)
            try:
                facts = LlmFacts.model_validate(data).to_facts()
            except ValidationError as err:
                messages += [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "record_facts",
                                "input": data,
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "is_error": True,
                                "content": f"Invalid, call record_facts again: {err.errors()}",
                            }
                        ],
                    },
                ]
                data, _ = await self._call(messages)
                facts = LlmFacts.model_validate(data).to_facts()
        except Exception as exc:  # network, API, or second validation failure
            self.usage.fallbacks += 1
            self.usage.errors.append(f"{posting.source}:{posting.external_id}: {exc!r}"[:300])
            log.warning("LLM extraction failed for %s, using rules: %r", posting.url, exc)
            return await self._fallback.extract(posting)
        return apply_hints(facts, posting)


_REGION_ALIASES = {
    "NA": "AMERICAS",
    "NORTH AMERICA": "AMERICAS",
    "LATAM": "AMERICAS",
    "LATIN AMERICA": "AMERICAS",
    "EUROPE": "EU",
    "ASIA": "APAC",
    "GLOBAL": "WORLD",
    "ANYWHERE": "WORLD",
    "WORLDWIDE": "WORLD",
    "UK": "GB",
}
_REGION_TOKENS = {"WORLD", "EU", "EMEA", "AMERICAS", "APAC", "CIS"}


def normalize_regions(values: list[str]) -> list[str]:
    """Map model output onto the closed region vocabulary; drop anything unrecognised."""
    out: list[str] = []
    for value in values:
        token = _REGION_ALIASES.get(value.strip().upper(), value.strip().upper())
        if (token in _REGION_TOKENS or (len(token) == 2 and token.isalpha())) and token not in out:
            out.append(token)
    return out


def apply_hints(facts: JobFacts, posting: RawPosting) -> JobFacts:
    """Source-provided structured data beats model output."""
    update: dict[str, Any] = {}
    hints = posting.hints
    if hints.get("years_min") is not None and facts.years_min is None:
        update["years_min"] = hints["years_min"]
    if hints.get("skills"):
        update["skills"] = list(dict.fromkeys([s.lower() for s in hints["skills"]] + facts.skills))
    if hints.get("countries") and not facts.countries:
        update["countries"] = hints["countries"]
    if posting.salary is not None:
        update["salary"] = posting.salary
    if posting.remote_hint is not RemotePolicy.UNKNOWN:
        update["remote"] = posting.remote_hint
    return facts.model_copy(update=update) if update else facts


def dumps_schema() -> str:
    return json.dumps(LlmFacts.model_json_schema(), indent=2)
