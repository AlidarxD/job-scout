"""Hacker News "Ask HN: Who is hiring?" monthly thread, via the public Algolia HN API.

Top-level comments follow a loose convention: "Company | Role | Location | REMOTE | $".
The first line is split on pipes; everything else goes to the extractor as description.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobscout.domain import Directness, RawPosting, RemotePolicy
from jobscout.http import get_json, parse_dt
from jobscout.normalize.text import html_to_text
from jobscout.sources.base import Source
from jobscout.sources.remote_boards import parse_money_range

ALGOLIA = "https://hn.algolia.com/api/v1"
_ROLE_WORDS = re.compile(
    r"engineer|developer|scientist|lead|manager|architect|sre|devops|ml|ai|analyst|designer"
    r"|founding|staff|principal|head of|cto|researcher",
    re.I,
)
_PLACE_HINT = re.compile(r"remote|onsite|on-site|hybrid|,\s*[A-Z]{2}\b|\b(usa?|uk|eu)\b", re.I)


class HackerNewsWhoIsHiringSource(Source):
    kind = "hn"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        search = await get_json(
            client,
            f"{ALGOLIA}/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 10},
        )
        thread = next(
            (h for h in search.get("hits", []) if "who is hiring" in h.get("title", "").lower()),
            None,
        )
        if thread is None:
            return
        item = await get_json(client, f"{ALGOLIA}/items/{thread['objectID']}")
        for posting in parse_hn_thread(item):
            yield posting


def parse_hn_thread(item: dict[str, Any]) -> list[RawPosting]:
    out = []
    for comment in item.get("children", []):
        text_html = comment.get("text")
        if not text_html or comment.get("author") is None:  # deleted / dead
            continue
        posting = parse_hn_comment(comment)
        if posting:
            out.append(posting)
    return out


def parse_hn_comment(comment: dict[str, Any]) -> RawPosting | None:
    text_html: str = comment["text"]
    text = html_to_text(text_html)
    header = text.split("\n", 1)[0]
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    company = parts[0]
    role = next((p for p in parts[1:] if _ROLE_WORDS.search(p)), parts[1])
    place = next((p for p in parts[1:] if p is not role and _PLACE_HINT.search(p)), None)
    links = [a.attributes.get("href") for a in HTMLParser(text_html).css("a[href]")]
    apply_url = next((link for link in links if link), None)
    low = header.lower()
    remote = (
        RemotePolicy.REMOTE
        if "remote" in low and "no remote" not in low
        else RemotePolicy.HYBRID
        if "hybrid" in low
        else RemotePolicy.ONSITE
        if "onsite" in low or "on-site" in low
        else RemotePolicy.UNKNOWN
    )
    cid = comment["id"]
    return RawPosting(
        source="hn",
        external_id=str(cid),
        url=f"https://news.ycombinator.com/item?id={cid}",
        apply_url=apply_url,
        title=role[:200],
        company=company[:120],
        description=text,
        location_raw=place,
        posted_at=parse_dt(comment.get("created_at_i") or comment.get("created_at")),
        remote_hint=remote,
        salary=parse_money_range(header) if "$" in header or "€" in header else None,
        directness=Directness.BOARD,  # posted by the employer themselves
        raw=comment,
    )
