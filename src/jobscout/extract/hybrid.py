"""Rules first, LLM only where the rules are unsure.

Most postings are fully handled by deterministic parsing (structured ATS/hh fields + regex).
The LLM is called only when a decision-relevant field is still unknown, or when the text
mentions a topic (citizenship, sponsorship, language, location limits) that the rules did
not resolve. Rule values always win; the LLM only fills gaps. This keeps cost and latency
proportional to ambiguity, and every LLM call has a logged reason.
"""

from __future__ import annotations

import re
from collections import Counter

from jobscout.domain import JobFacts, RawPosting, RemotePolicy, Seniority
from jobscout.extract import Extractor
from jobscout.extract.rules import RuleExtractor

_TOPICS: dict[str, re.Pattern[str]] = {
    "visa": re.compile(r"sponsor|visa|work permit|right to work|authori[sz]ed to work", re.I),
    "citizenship": re.compile(r"national|citizen|clearance|гражданств", re.I),
    "language": re.compile(r"fluent|native|proficien|JLPT|язык", re.I),
    "regions": re.compile(r"must (?:be )?(?:based|located|reside)|time ?zone|only\b", re.I),
}


def uncertain_fields(facts: JobFacts, posting: RawPosting) -> list[str]:
    text = posting.description
    reasons = []
    if facts.seniority is Seniority.UNKNOWN:
        reasons.append("seniority")
    if facts.remote is RemotePolicy.UNKNOWN:
        reasons.append("remote")
    if facts.visa_sponsorship is None and _TOPICS["visa"].search(text):
        reasons.append("visa")
    if not facts.citizenship_required and _TOPICS["citizenship"].search(text):
        reasons.append("citizenship")
    if not facts.required_languages and _TOPICS["language"].search(text):
        reasons.append("language")
    if (
        facts.remote is RemotePolicy.REMOTE
        and not facts.remote_regions
        and _TOPICS["regions"].search(text)
    ):
        reasons.append("regions")
    return reasons


class HybridExtractor:
    def __init__(self, llm: Extractor) -> None:
        self.rules = RuleExtractor()
        self.llm = llm
        self.name = f"hybrid:{self.rules.name}+{llm.name}"
        self.routed: Counter[str] = Counter()
        self.total = 0

    async def extract(self, posting: RawPosting) -> JobFacts:
        self.total += 1
        facts = self.rules.extract_sync(posting)
        reasons = uncertain_fields(facts, posting)
        if not reasons:
            return facts
        self.routed.update(reasons)
        self.routed["_llm_calls"] += 1
        llm_facts = await self.llm.extract(posting)
        return merge_facts(facts, llm_facts)


def merge_facts(rules: JobFacts, llm: JobFacts) -> JobFacts:
    """Rule values win; LLM fills only what rules left unknown/empty."""
    update: dict[str, object] = {}
    for name in JobFacts.model_fields:
        mine, theirs = getattr(rules, name), getattr(llm, name)
        empty = mine in (None, [], Seniority.UNKNOWN, RemotePolicy.UNKNOWN)
        if empty and theirs not in (None, [], Seniority.UNKNOWN, RemotePolicy.UNKNOWN):
            update[name] = theirs
    return rules.model_copy(update=update) if update else rules
