from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobscout.scoring.profile import CandidateProfile
from jobscout.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    text = (FIXTURES / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, storage="memory")  # type: ignore[call-arg]


@pytest.fixture
def profile() -> CandidateProfile:
    return CandidateProfile.load(Path(__file__).parents[1] / "config" / "profile.example.yaml")
