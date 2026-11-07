from __future__ import annotations

from typing import Protocol

from jobscout.domain import JobFacts, RawPosting


class Extractor(Protocol):
    name: str

    async def extract(self, posting: RawPosting) -> JobFacts: ...
