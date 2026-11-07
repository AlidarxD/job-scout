"""Strict 0-10 fit scoring with hard gates.

Two stages, both explainable:
1. Gates: deal-breakers that make a posting unusable for this candidate no matter how good
   the match (closed, nationals-only, required language, country-locked remote...). A gated
   job is kept in a "rejected with reasons" list, never silently dropped.
2. Rubric: six dimensions scored 0-10 and combined with fixed weights. Calibration is
   strict - 5 means "plausible but unremarkable", 8+ means "apply today". Unknowns score
   a neutral 5, never a hopeful 8.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from jobscout.domain import Job, JobStatus, RemotePolicy, Seniority
from jobscout.normalize.dedup import normalize_company
from jobscout.normalize.geo import country_in_regions
from jobscout.scoring.profile import CandidateProfile

WEIGHTS = {
    "role": 0.25,
    "skills": 0.25,
    "seniority": 0.15,
    "location": 0.15,
    "domain": 0.10,
    "compensation": 0.10,
}
LANG_NAMES = {
    "ja": "Japanese",
    "de": "German",
    "fr": "French",
    "ar": "Arabic",
    "ko": "Korean",
    "zh": "Chinese",
    "pl": "Polish",
    "nl": "Dutch",
    "es": "Spanish",
    "ru": "Russian",
    "kk": "Kazakh",
    "pt": "Portuguese",
    "it": "Italian",
    "tr": "Turkish",
}


class Gate(BaseModel):
    code: str
    reason: str


class ScoreResult(BaseModel):
    job_id: str
    profile_id: str
    score: float  # 0-10, one decimal
    passed: bool  # no gate fired and score >= profile.fit_threshold
    gates: list[Gate] = Field(default_factory=list)
    dimensions: dict[str, float] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    why_it_works: str = ""  # e.g. "remote worldwide" / "visa sponsorship + relocation"


def _company_in(company: str, names: list[str]) -> bool:
    norm = normalize_company(company)
    return any(normalize_company(n) == norm for n in names)


def gates(job: Job, profile: CandidateProfile) -> list[Gate]:
    f = job.facts
    out: list[Gate] = []
    if job.status is JobStatus.CLOSED:
        out.append(Gate(code="closed", reason="posting is closed"))
    if _company_in(job.company, profile.blacklist):
        out.append(Gate(code="blacklist", reason=f"{job.company} is on your blacklist"))
    if f.citizenship_required and not set(f.citizenship_required) & set(profile.citizenship):
        out.append(
            Gate(
                code="citizenship",
                reason=f"restricted to nationals/clearance: {', '.join(f.citizenship_required)}",
            )
        )
    missing = [lang for lang in f.required_languages if lang not in profile.languages]
    if missing:
        out.append(
            Gate(
                code="language",
                reason="requires " + ", ".join(LANG_NAMES.get(m, m) for m in missing),
            )
        )
    if f.remote is RemotePolicy.REMOTE:
        if not profile.location.remote_ok:
            out.append(Gate(code="remote", reason="remote role but you want on-site"))
        elif f.remote_regions and not country_in_regions(profile.based_in, f.remote_regions):
            out.append(
                Gate(code="remote_region", reason=f"remote only for {', '.join(f.remote_regions)}")
            )
    else:
        places = f.countries
        home_ok = {profile.based_in, *profile.location.onsite_countries}
        reachable = home_ok | set(profile.location.relocate_to)
        if places and not set(places) & reachable:
            out.append(
                Gate(
                    code="geography",
                    reason=f"on-site in {', '.join(places)}, outside your target countries",
                )
            )
        abroad = bool(places) and not set(places) & home_ok
        if abroad and profile.location.needs_visa_sponsorship and f.visa_sponsorship is False:
            out.append(Gate(code="visa", reason="needs work permit, no visa sponsorship"))
    if f.seniority is not Seniority.UNKNOWN:
        if f.seniority < profile.seniority_min - 1:
            out.append(Gate(code="too_junior", reason=f"{f.seniority.label} role"))
        if f.seniority > profile.seniority_max:
            out.append(Gate(code="too_senior", reason=f"{f.seniority.label} role"))
    if profile.salary_floor_usd_month and f.salary:
        _, hi = f.salary.monthly_usd()
        if hi is not None and hi < profile.salary_floor_usd_month * 0.85:
            out.append(Gate(code="salary", reason=f"tops out at ~${hi:,.0f}/mo"))
    return out


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w+#.]+", text.lower()))


def score_role(job: Job, profile: CandidateProfile) -> tuple[float, str | None]:
    title = job.title.lower()
    for role in profile.target_roles:
        if role.lower() in title:
            return 10.0, f"title matches '{role}'"
    title_words = _words(title)
    role_words = {w for r in profile.target_roles for w in _words(r)} - {"engineer", "senior"}
    keywords = {k.lower() for k in profile.role_keywords}
    hits = (title_words & role_words) | {k for k in keywords if k in title}
    if not hits:
        body_hits = [k for k in keywords if k in job.description.lower()[:3000]]
        return (4.0 if body_hits else 1.0), None
    return min(9.0, 5.0 + 2.0 * len(hits)), f"title mentions {', '.join(sorted(hits))}"


def score_skills(job: Job, profile: CandidateProfile) -> tuple[float, list[str], list[str]]:
    wanted = set(job.facts.skills)
    if not wanted:
        return 5.0, [], []
    core = {s.lower() for s in profile.core_skills}
    have = profile.all_skills
    matched = wanted & have
    missing = sorted(wanted - have)
    core_hits = len(wanted & core)
    coverage = len(matched) / len(wanted)
    score = 10 * coverage * 0.7 + min(core_hits, 5) * 0.6
    return round(min(10.0, score), 1), sorted(matched), missing


def score_seniority(job: Job, profile: CandidateProfile) -> float:
    level = job.facts.seniority
    if level is Seniority.UNKNOWN:
        return 5.0
    if profile.seniority_min <= level <= profile.seniority_max:
        return 10.0
    return 5.0 if abs(level - profile.seniority_min) == 1 else 1.0


def score_location(job: Job, profile: CandidateProfile) -> tuple[float, str]:
    f = job.facts
    home = profile.based_in
    if f.remote is RemotePolicy.REMOTE:
        if not f.remote_regions or "WORLD" in f.remote_regions:
            return 10.0, "remote worldwide"
        if home in f.remote_regions:
            return 10.0, f"remote open to {home}"
        if country_in_regions(home, f.remote_regions):
            return 8.0, f"remote within {', '.join(f.remote_regions)}"
        return 0.0, ""
    if home in f.countries or set(f.countries) & set(profile.location.onsite_countries):
        return 10.0, f"office in {home}" if home in f.countries else "office in a home-OK country"
    if set(f.countries) & set(profile.location.relocate_to):
        perks = [
            p
            for p, ok in (
                ("visa sponsorship", f.visa_sponsorship),
                ("relocation support", f.relocation),
            )
            if ok
        ]
        if perks:
            return 9.0, " + ".join(perks)
        return 5.0, "relocation target, sponsorship not stated"
    return (5.0, "location not stated") if not f.countries else (1.0, "")


def score_domain(job: Job, profile: CandidateProfile) -> float:
    if not profile.domains:
        return 5.0
    text = f"{job.title} {' '.join(job.tags)} {job.description[:4000]}".lower()
    hits = sum(1 for d in profile.domains if d.lower() in text)
    return 10.0 if hits >= 2 else 8.0 if hits == 1 else 3.0


def score_compensation(job: Job, profile: CandidateProfile) -> float:
    if not job.facts.salary or not (
        profile.salary_floor_usd_month or profile.salary_target_usd_month
    ):
        return 5.0
    lo, hi = job.facts.salary.monthly_usd()
    top = hi or lo
    if top is None:
        return 5.0
    target = profile.salary_target_usd_month or (profile.salary_floor_usd_month or 0) * 1.3
    floor = profile.salary_floor_usd_month or target * 0.7
    if top >= target:
        return 10.0
    if top >= floor:
        return 7.0
    return 2.0


def score(job: Job, profile: CandidateProfile) -> ScoreResult:
    gate_list = gates(job, profile)
    role, role_note = score_role(job, profile)
    skills, matched, missing = score_skills(job, profile)
    location, why = score_location(job, profile)
    dims = {
        "role": role,
        "skills": skills,
        "seniority": score_seniority(job, profile),
        "location": location,
        "domain": score_domain(job, profile),
        "compensation": score_compensation(job, profile),
    }
    total = sum(dims[k] * w for k, w in WEIGHTS.items())
    if _company_in(job.company, profile.watchlist):
        total = min(10.0, total + 0.5)
    total = round(total, 1)
    strengths = [s for s in (role_note, f"skills: {', '.join(matched)}" if matched else None) if s]
    gaps = [f"missing: {', '.join(missing[:8])}"] if missing else []
    if job.facts.years_min and job.facts.years_min >= 5 and job.facts.seniority <= Seniority.MID:
        gaps.append(f"asks {job.facts.years_min}+ years")
    return ScoreResult(
        job_id=job.id,
        profile_id=profile.id,
        score=total,
        passed=not gate_list and total >= profile.fit_threshold,
        gates=gate_list,
        dimensions=dims,
        strengths=strengths,
        gaps=gaps,
        why_it_works=why,
    )
