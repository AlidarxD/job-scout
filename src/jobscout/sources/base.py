from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx

from jobscout.domain import Directness, RawPosting, Scope
from jobscout.settings import Settings


class Source(ABC):
    """One configured feed, e.g. `greenhouse:stripe` or `hh:python almaty`.

    Subclasses keep network code in `fetch` and mapping in pure `parse*` functions so that
    every mapping is covered by fixture tests without touching the network.
    """

    kind: ClassVar[str]
    scope: ClassVar[Scope] = Scope.GLOBAL
    directness: ClassVar[Directness] = Directness.AGGREGATOR

    def __init__(self, settings: Settings, **options: Any) -> None:
        self.settings = settings
        self.options = options

    @property
    def key(self) -> str:
        """Unique id of this configured feed; used for run bookkeeping."""
        return self.kind

    @abstractmethod
    def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]: ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.key}>"
