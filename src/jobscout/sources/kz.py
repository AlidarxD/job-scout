"""Kazakhstan sources: hh.kz (official API), public Telegram job channels, plain HTML boards."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from jobscout.domain import Directness, RawPosting, RemotePolicy, Salary, Scope
from jobscout.extract.rules import parse_salary_text
from jobscout.http import get_json, parse_dt, request
from jobscout.normalize.salary import canonical_currency
from jobscout.normalize.text import html_to_text
from jobscout.sources.base import Source
from jobscout.sources.remote_boards import parse_money_range

# --- hh.kz -------------------------------------------------------------------------------

HH_API = "https://api.hh.ru"
HH_AREA_KAZAKHSTAN = 40
_HH_EXPERIENCE_YEARS = {"noExperience": 0, "between1And3": 1, "between3And6": 3, "moreThan6": 6}
_HH_PERIOD = {"MONTH": "month", "YEAR": "year", "HOUR": "hour", "DAY": "day", "WEEK": "week"}


class HeadHunterSource(Source):
    """hh.kz vacancies through api.hh.ru (`host=hh.kz` so links point at hh.kz).

    Options: text (search query), area (default 40 = Kazakhstan; 160 Almaty, 159 Astana),
    period (days, ≤30), max_pages, max_details (vacancy cards fetched for full description).
    """

    kind = "hh"
    scope = Scope.LOCAL
    directness = Directness.BOARD

    @property
    def key(self) -> str:
        return f"hh:{self.options.get('area', HH_AREA_KAZAKHSTAN)}:{self.options.get('text', '')}"

    def _headers(self) -> dict[str, str]:
        headers = {"HH-User-Agent": self.settings.user_agent}
        if self.settings.hh_token:
            headers["Authorization"] = f"Bearer {self.settings.hh_token.get_secret_value()}"
        return headers

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        headers = self._headers()
        params: dict[str, Any] = {
            "text": self.options.get("text", ""),
            "area": self.options.get("area", HH_AREA_KAZAKHSTAN),
            "period": self.options.get("period", 30),
            "per_page": 100,
            "host": "hh.kz",
            "order_by": "publication_time",
        }
        items: list[dict[str, Any]] = []
        for page in range(int(self.options.get("max_pages", 5))):
            data = await get_json(
                client, f"{HH_API}/vacancies", params={**params, "page": page}, headers=headers
            )
            items.extend(data.get("items", []))
            if page + 1 >= int(data.get("pages", 0)):
                break

        sem = asyncio.Semaphore(3)  # hh throttles bursts hard (captcha on abuse)

        async def card(vacancy_id: str) -> dict[str, Any] | None:
            async with sem:
                try:
                    data: dict[str, Any] = await get_json(
                        client,
                        f"{HH_API}/vacancies/{vacancy_id}",
                        params={"host": "hh.kz"},
                        headers=headers,
                    )
                    return data
                except httpx.HTTPError:
                    return None

        max_details = int(self.options.get("max_details", 50))
        cards = await asyncio.gather(*(card(i["id"]) for i in items[:max_details]))
        details = {c["id"]: c for c in cards if c}
        for item in items:
            yield parse_hh_item(item, details.get(item["id"]))


def parse_hh_item(item: dict[str, Any], detail: dict[str, Any] | None = None) -> RawPosting:
    src = detail or item
    sal = src.get("salary_range") or src.get("salary")
    salary = None
    if sal and (sal.get("from") or sal.get("to")):
        salary = Salary(
            min=sal.get("from"),
            max=sal.get("to"),
            currency=canonical_currency(sal.get("currency") or "KZT"),
            period=_HH_PERIOD.get(((sal.get("mode") or {}).get("id")) or "MONTH", "month"),
            gross=sal.get("gross"),
        )
    formats = {f.get("id") for f in src.get("work_format") or []}
    schedule = (src.get("schedule") or {}).get("id")
    remote = (
        RemotePolicy.REMOTE
        if "REMOTE" in formats or schedule == "remote"
        else RemotePolicy.HYBRID
        if "HYBRID" in formats
        else RemotePolicy.ONSITE
        if "ON_SITE" in formats or schedule == "fullDay"
        else RemotePolicy.UNKNOWN
    )
    if detail and detail.get("description"):
        description = html_to_text(detail["description"])
    else:
        snippet = item.get("snippet") or {}
        description = "\n".join(
            html_to_text(snippet.get(k))
            for k in ("responsibility", "requirement")
            if snippet.get(k)
        )
    hints: dict[str, Any] = {"countries": ["KZ"]}
    exp = (src.get("experience") or {}).get("id")
    if exp in _HH_EXPERIENCE_YEARS:
        hints["years_min"] = _HH_EXPERIENCE_YEARS[exp]
    if detail and detail.get("key_skills"):
        hints["skills"] = [s["name"] for s in detail["key_skills"]]
    area = (src.get("area") or {}).get("name")
    return RawPosting(
        source="hh",
        external_id=str(item["id"]),
        url=item.get("alternate_url") or f"https://hh.kz/vacancy/{item['id']}",
        apply_url=item.get("apply_alternate_url"),
        title=item["name"].strip(),
        company=((item.get("employer") or {}).get("name") or "Unknown").strip(),
        description=description,
        location_raw=f"{area}, Kazakhstan" if area and area != "Казахстан" else "Kazakhstan",
        posted_at=parse_dt(item.get("published_at")),
        remote_hint=remote,
        country_hint="KZ",
        salary=salary,
        tags=[r["name"] for r in item.get("professional_roles") or [] if r.get("name")],
        hints=hints,
        directness=Directness.BOARD,
        raw=item,
    )


# --- Telegram public channels --------------------------------------------------------------

_VACANCY_MARKERS = re.compile(
    r"#(вакансия|vacancy|job|jobs|hiring|работа|ищем)|\b(вакансия|vacancy|we are hiring|"
    r"we're hiring|ищем|требуется|hiring)\b",
    re.I,
)
_FIELD = {
    "company": re.compile(
        r"^(?:компания|company|работодатель|employer)\s*[:：-]\s*(.+)$", re.I | re.M
    ),
    "location": re.compile(
        r"^(?:локация|город|location|city|место работы|где)\s*[:：-]\s*(.+)$", re.I | re.M
    ),
    "title": re.compile(
        r"^(?:вакансия|позиция|position|role|должность)\s*[:：-]\s*(.+)$", re.I | re.M
    ),
    "salary": re.compile(r"^(?:зарплата|зп|з/п|salary|вилка|оклад)\s*[:：-]\s*(.+)$", re.I | re.M),
}
_HASHTAG_LINE = re.compile(r"^(#\S+\s*)+$")


class TelegramChannelSource(Source):
    """Public channel preview at https://t.me/s/<channel> — no bot, no login, no API key."""

    kind = "telegram"
    scope = Scope.LOCAL
    directness = Directness.BOARD

    @property
    def channel(self) -> str:
        return str(self.options["channel"]).lstrip("@")

    @property
    def key(self) -> str:
        return f"telegram:{self.channel}"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        before: int | None = None
        for _ in range(int(self.options.get("max_pages", 3))):
            params = {"before": before} if before else None
            resp = await request(
                client,
                "GET",
                f"https://t.me/s/{self.channel}",
                params=params,
                headers={"Accept": "text/html"},
            )
            postings, oldest = parse_telegram_page(resp.text, self.channel)
            for posting in postings:
                yield posting
            if oldest is None or oldest <= 1:
                break
            before = oldest


def parse_telegram_page(html: str, channel: str) -> tuple[list[RawPosting], int | None]:
    tree = HTMLParser(html)
    out: list[RawPosting] = []
    ids: list[int] = []
    for msg in tree.css("div.tgme_widget_message[data-post]"):
        post = msg.attributes.get("data-post") or ""
        try:
            msg_id = int(post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        ids.append(msg_id)
        text_node = msg.css_first("div.tgme_widget_message_text")
        if text_node is None:
            continue
        text = html_to_text(text_node.html)
        if not _VACANCY_MARKERS.search(text):
            continue
        posting = parse_telegram_text(text, channel, msg_id, _msg_time(msg), _links(text_node))
        if posting:
            out.append(posting)
    return out, (min(ids) if ids else None)


def _msg_time(msg: Node) -> str | None:
    node = msg.css_first("time[datetime]")
    return node.attributes.get("datetime") if node else None


def _links(node: Node) -> list[str]:
    hrefs = [a.attributes.get("href") or "" for a in node.css("a[href]")]
    return [h for h in hrefs if h.startswith("http") and "t.me/" not in h]


def parse_telegram_text(
    text: str, channel: str, msg_id: int, posted: str | None, links: list[str]
) -> RawPosting | None:
    fields = {
        k: (m.group(1).strip() if (m := rx.search(text)) else None) for k, rx in _FIELD.items()
    }
    lines = [ln.strip(" *•-—") for ln in text.splitlines() if ln.strip()]
    content = [ln for ln in lines if not _HASHTAG_LINE.match(ln)]
    title = fields["title"] or (content[0] if content else None)
    if not title:
        return None
    title = re.sub(r"^#?(вакансия|vacancy)\s*[:：-]?\s*", "", title, flags=re.I).strip() or title
    salary = parse_salary_text(fields["salary"]) if fields["salary"] else None
    if salary is None and fields["salary"]:
        salary = parse_money_range(fields["salary"])
    return RawPosting(
        source="telegram",
        external_id=f"{channel}/{msg_id}",
        url=f"https://t.me/{channel}/{msg_id}",
        apply_url=links[0] if links else None,
        title=title[:200],
        company=(fields["company"] or "Unknown")[:120],
        description=text,
        location_raw=fields["location"],
        posted_at=parse_dt(posted),
        salary=salary,
        tags=[f"tg:{channel}"],
        directness=Directness.BOARD,
        raw={"channel": channel, "id": msg_id},
    )


# --- Generic HTML listing (dsml.kz and similar) ---------------------------------------------


class HtmlBoardSource(Source):
    """Config-driven scraper for small boards without an API.

    Options: name, url, item (CSS), title, link, company?, location?, date? (CSS, relative to
    item), base_url?. Only listing pages are parsed — no detail-page crawling by default, so
    the load on the site is one request per run.
    """

    kind = "html"
    scope = Scope.LOCAL
    directness = Directness.BOARD

    @property
    def key(self) -> str:
        return f"html:{self.options['name']}"

    async def fetch(self, client: httpx.AsyncClient) -> AsyncIterator[RawPosting]:
        resp = await request(client, "GET", self.options["url"], headers={"Accept": "text/html"})
        for posting in parse_html_board(resp.text, self.options):
            yield posting


def parse_html_board(html: str, opts: dict[str, Any]) -> list[RawPosting]:
    tree = HTMLParser(html)
    base = str(opts.get("base_url") or opts["url"]).rstrip("/")
    out = []

    def pick(node: Node, selector: str | None) -> str | None:
        if not selector:
            return None
        found = node.css_first(selector)
        if found is None:
            return None
        return (found.attributes.get("datetime") or found.text(strip=True)) or None

    for node in tree.css(opts["item"]):
        title = pick(node, opts["title"])
        link_node = node.css_first(opts["link"]) if opts.get("link") else None
        href = link_node.attributes.get("href") if link_node else None
        if not title or not href:
            continue
        url = href if href.startswith("http") else f"{base}/{href.lstrip('/')}"
        location = pick(node, opts.get("location"))
        out.append(
            RawPosting(
                source=f"html:{opts['name']}",
                external_id=url,
                url=url,
                title=title,
                company=pick(node, opts.get("company"))
                or str(opts.get("default_company", "Unknown")),
                description=node.text(separator="\n", strip=True),
                location_raw=location or opts.get("default_location"),
                posted_at=parse_dt(pick(node, opts.get("date"))),
                country_hint=opts.get("country"),
                directness=Directness.BOARD,
            )
        )
    return out
