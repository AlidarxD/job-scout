from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from jobscout.domain import Seniority


class LocationPrefs(BaseModel):
    remote_ok: bool = True
    # Countries where the candidate would work on-site (home country included automatically).
    onsite_countries: list[str] = Field(default_factory=list)
    # Countries the candidate would relocate to (on-site / hybrid abroad).
    relocate_to: list[str] = Field(default_factory=list)
    needs_visa_sponsorship: bool = True


class CandidateProfile(BaseModel):
    """Saved, reusable search profile (gregorymm-style onboarding writes one of these)."""

    id: str = "default"
    name: str = ""
    based_in: str = "KZ"  # ISO2, where the candidate lives now
    citizenship: list[str] = Field(default_factory=lambda: ["KZ"])
    languages: list[str] = Field(default_factory=lambda: ["en"])  # ISO-639-1
    target_roles: list[str] = Field(default_factory=list)  # "AI Engineer", "Backend Engineer"
    role_keywords: list[str] = Field(default_factory=list)  # "llm", "platform", "backend"
    seniority_min: Seniority = Seniority.MID
    seniority_max: Seniority = Seniority.LEAD
    core_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)  # "fintech", "ai infra", "e-commerce"
    location: LocationPrefs = Field(default_factory=LocationPrefs)
    salary_floor_usd_month: float | None = None
    salary_target_usd_month: float | None = None
    watchlist: list[str] = Field(default_factory=list)  # companies to boost
    blacklist: list[str] = Field(default_factory=list)  # companies / agencies to drop
    fit_threshold: float = 6.0

    @field_validator("seniority_min", "seniority_max", mode="before")
    @classmethod
    def _parse_level(cls, value: object) -> object:
        if isinstance(value, str):
            return Seniority[value.upper()]
        return value

    @field_validator("based_in")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @classmethod
    def load(cls, path: Path) -> CandidateProfile:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    @property
    def all_skills(self) -> set[str]:
        return {s.lower() for s in self.core_skills + self.secondary_skills}
