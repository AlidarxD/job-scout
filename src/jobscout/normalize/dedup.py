"""Cross-source de-duplication.

The same vacancy routinely shows up on the employer ATS, LinkedIn, two remote boards and a
Telegram channel. We collapse them with a fingerprint over (company, title, place) and keep
the most direct source as canonical, so users never get an aggregator-only link when the
employer's own page exists.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from jobscout.domain import Job, RawPosting, SourceRef
from jobscout.normalize.text import slug

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|bv|b v|plc|corp|corporation|co|"
    r"тоо|ао|ооо|too|jsc|llp|kk|k k|株式会社)\b\.?",
    re.I,
)
_TITLE_NOISE = re.compile(
    r"\((remote|hybrid|onsite|on-site|f/m/d|m/f/d|m/w/d|w/m/d|all genders)[^)]*\)"
    r"|\b(remote|hybrid|удаленно|удалённо|удаленка)\b",
    re.I,
)
_TITLE_LOCATION_TAIL = re.compile(r"\s[-–—|@,]\s.*$")


def normalize_company(name: str) -> str:
    return slug(_LEGAL_SUFFIXES.sub(" ", name.lower()))


def normalize_title(title: str) -> str:
    title = _TITLE_NOISE.sub(" ", title)
    title = _TITLE_LOCATION_TAIL.sub("", title)
    title = title.replace("Sr.", "Senior").replace("Jr.", "Junior")
    return slug(title)


def fingerprint(company: str, title: str, place: str | None) -> str:
    key = "|".join([normalize_company(company), normalize_title(title), (place or "any").upper()])
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]


def merge(existing: Job, posting: RawPosting) -> Job:
    """Fold another sighting of the same vacancy into an existing Job."""
    ref = SourceRef(
        source=posting.source,
        external_id=posting.external_id,
        url=posting.url,
        directness=posting.directness,
    )
    sources = [
        s for s in existing.sources if (s.source, s.external_id) != (ref.source, ref.external_id)
    ]
    sources.append(ref)
    best = max(sources, key=lambda s: s.directness)
    update: dict[str, object] = {"sources": sources, "last_seen": max(existing.last_seen, _now())}
    if best is ref:
        update["url"] = posting.url
        update["apply_url"] = posting.apply_url or existing.apply_url
        # A more direct source usually has the fuller description.
        if len(posting.description) > len(existing.description):
            update["description"] = posting.description
    if posting.posted_at and (existing.posted_at is None or posting.posted_at < existing.posted_at):
        update["posted_at"] = posting.posted_at
    update["tags"] = sorted(set(existing.tags) | set(posting.tags))
    return existing.model_copy(update=update)


def _now() -> datetime:
    return datetime.now(UTC)
