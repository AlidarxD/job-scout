from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from jobscout.settings import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


def make_client(settings: Settings, **headers: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_s,
        headers={"User-Agent": settings.user_agent, "Accept": "application/json", **headers},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=settings.http_max_concurrency),
    )


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    **kwargs: Any,
) -> httpx.Response:
    """HTTP call with backoff on 429/5xx that honours Retry-After."""
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == attempts:
                raise
            log.warning("transport error %s on %s, retrying", exc, url)
        else:
            if resp.status_code not in RETRY_STATUSES or attempt == attempts:
                resp.raise_for_status()
                return resp
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(float(retry_after), 60.0)
            log.warning("%s on %s, retry %d in %.0fs", resp.status_code, url, attempt, delay)
        await asyncio.sleep(delay)
        delay *= 2
    raise AssertionError("unreachable")


async def get_json(client: httpx.AsyncClient, url: str, **kwargs: Any) -> Any:
    return (await request(client, "GET", url, **kwargs)).json()


def parse_dt(value: Any) -> datetime | None:
    """Parse the zoo of timestamp formats job APIs use into an aware UTC datetime."""
    if value in (None, "", 0):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value  # ms vs s epoch
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(value).strip()
    if text.isdigit():
        return parse_dt(int(text))
    text = text.replace("Z", "+00:00")
    # hh.ru uses "+0300" without a colon; fromisoformat handles it on 3.11+, but be explicit.
    if len(text) > 5 and text[-5] in "+-" and text[-4:].isdigit() and ":" not in text[-5:]:
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
