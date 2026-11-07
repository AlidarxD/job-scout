"""Source registry and config loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from jobscout.settings import Settings
from jobscout.sources.ats import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    SmartRecruitersSource,
    WorkableSource,
)
from jobscout.sources.base import Source
from jobscout.sources.hn import HackerNewsWhoIsHiringSource
from jobscout.sources.kz import HeadHunterSource, HtmlBoardSource, TelegramChannelSource
from jobscout.sources.linkedin import LinkedInApifySource
from jobscout.sources.remote_boards import (
    ArbeitnowSource,
    HimalayasSource,
    RemoteOkSource,
    RemotiveSource,
)

REGISTRY: dict[str, type[Source]] = {
    cls.kind: cls
    for cls in (
        GreenhouseSource,
        LeverSource,
        AshbySource,
        WorkableSource,
        SmartRecruitersSource,
        HeadHunterSource,
        TelegramChannelSource,
        HtmlBoardSource,
        RemotiveSource,
        RemoteOkSource,
        ArbeitnowSource,
        HimalayasSource,
        HackerNewsWhoIsHiringSource,
        LinkedInApifySource,
    )
}


def load_sources(settings: Settings, path: Path | None = None) -> list[Source]:
    """Build Source instances from config/sources.yaml.

    Format: a list under `sources:`; each entry has `kind`, optional `enabled: false`, and
    kind-specific options. `companies:` entries expand to one ATS source each.
    """
    config: dict[str, Any] = (
        yaml.safe_load((path or settings.sources_file).read_text(encoding="utf-8")) or {}
    )
    entries: list[dict[str, Any]] = list(config.get("sources") or [])
    for company in config.get("companies") or []:
        for ats in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"):
            if board := company.get(ats):
                entries.append(
                    {
                        "kind": ats,
                        "board": board,
                        "company": company["name"],
                        "enabled": company.get("enabled", True),
                    }
                )
    sources: list[Source] = []
    for entry in entries:
        entry = dict(entry)
        if not entry.pop("enabled", True):
            continue
        kind = entry.pop("kind")
        if kind not in REGISTRY:
            raise ValueError(f"unknown source kind {kind!r}; known: {sorted(REGISTRY)}")
        sources.append(REGISTRY[kind](settings, **entry))
    return sources


__all__ = ["REGISTRY", "Source", "load_sources"]
