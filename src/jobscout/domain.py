"""Core domain types shared by sources, extraction, scoring, storage and the API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Scope(StrEnum):
    LOCAL = "local"  # Kazakhstan
    GLOBAL = "global"


class RemotePolicy(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class Seniority(IntEnum):
    UNKNOWN = 0
    INTERN = 1
    JUNIOR = 2
    MID = 3
    SENIOR = 4
    LEAD = 5
    PRINCIPAL = 6

    @property
    def label(self) -> str:
        return self.name.lower()


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class Directness(IntEnum):
    """How close a source is to the employer. Higher wins when duplicates merge."""

    AGGREGATOR = 1  # re-posts from elsewhere (remote boards, LinkedIn scrapes, HN)
    BOARD = 2  # employer posts directly on a job board (hh.kz, Telegram channel)
    EMPLOYER_ATS = 3  # employer's own applicant tracking system


class Salary(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str = "USD"
    period: str = "year"  # year | month | hour
    gross: bool | None = None

    def monthly_usd(self) -> tuple[float | None, float | None]:
        from jobscout.normalize.salary import to_monthly_usd

        return to_monthly_usd(self)


class RawPosting(BaseModel):
    """A vacancy exactly as one source returned it, mapped to common fields."""

    source: str
    external_id: str
    url: str
    title: str
    company: str
    description: str = ""
    location_raw: str | None = None
    apply_url: str | None = None
    posted_at: datetime | None = None
    remote_hint: RemotePolicy = RemotePolicy.UNKNOWN
    country_hint: str | None = None  # ISO-3166 alpha-2 if the source says so
    salary: Salary | None = None
    tags: list[str] = Field(default_factory=list)
    directness: Directness = Directness.AGGREGATOR
    # Structured hints some sources give for free (hh "experience", Himalayas "seniority",
    # hh key_skills). Extractors trust these over text heuristics. Known keys:
    # years_min:int, seniority:str, skills:list[str], countries:list[str], remote_regions:list[str]
    hints: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


class JobFacts(BaseModel):
    """Structured facts pulled out of a posting (by rules or by an LLM)."""

    seniority: Seniority = Seniority.UNKNOWN
    years_min: int | None = None
    remote: RemotePolicy = RemotePolicy.UNKNOWN
    # Where a remote hire may live: ISO2 codes plus region tokens WORLD, EU, EMEA, AMERICAS, APAC.
    remote_regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)  # office countries, ISO2
    visa_sponsorship: bool | None = None
    relocation: bool | None = None
    # Citizenship / clearance the role demands (Saudization, Emiratisation, US clearance...).
    citizenship_required: list[str] = Field(default_factory=list)  # ISO2
    required_languages: list[str] = Field(default_factory=list)  # ISO-639-1, besides English
    skills: list[str] = Field(default_factory=list)
    salary: Salary | None = None


class SourceRef(BaseModel):
    source: str
    external_id: str
    url: str
    directness: Directness


class Job(BaseModel):
    """A de-duplicated vacancy. One Job may be seen on several sources."""

    id: str  # stable fingerprint, see normalize.dedup
    title: str
    company: str
    url: str  # canonical (most direct) posting URL
    apply_url: str | None = None
    description: str = ""
    location_raw: str | None = None
    posted_at: datetime | None = None
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: JobStatus = JobStatus.OPEN
    scope: Scope = Scope.GLOBAL
    tags: list[str] = Field(default_factory=list)
    facts: JobFacts = Field(default_factory=JobFacts)
    sources: list[SourceRef] = Field(default_factory=list)

    @property
    def primary_country(self) -> str | None:
        return self.facts.countries[0] if self.facts.countries else None

    def age_days(self, now: datetime | None = None) -> int | None:
        if self.posted_at is None:
            return None
        now = now or datetime.now(UTC)
        posted = self.posted_at if self.posted_at.tzinfo else self.posted_at.replace(tzinfo=UTC)
        return max(0, (now - posted).days)
