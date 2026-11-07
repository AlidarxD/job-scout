from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from jobscout.extract import Extractor
from jobscout.extract.rules import RuleExtractor
from jobscout.http import make_client
from jobscout.settings import Settings, get_settings
from jobscout.storage.base import Repository

app = typer.Typer(help="Job Scout: crawl, score and track vacancies.", no_args_is_help=True)
sources_app = typer.Typer(help="Inspect configured sources.")
eval_app = typer.Typer(help="Offline evaluations.")
app.add_typer(sources_app, name="sources")
app.add_typer(eval_app, name="eval")


def _repo(settings: Settings, storage: str | None = None) -> Repository:
    if (storage or settings.storage) == "memory":
        from jobscout.storage.memory import InMemoryRepository

        return InMemoryRepository()
    from jobscout.storage.sql import SqlRepository

    return SqlRepository.from_url(settings.database_url)


def _extractor(settings: Settings, name: str) -> Extractor:
    if name == "llm":
        if settings.anthropic_api_key is None:
            typer.echo("JOBSCOUT_ANTHROPIC_API_KEY is not set; use --extractor rules", err=True)
            raise typer.Exit(2)
        from jobscout.extract.llm import LlmExtractor

        return LlmExtractor.from_settings(
            settings.anthropic_api_key.get_secret_value(), settings.llm_model
        )
    return RuleExtractor()


@app.callback()
def main(verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command()
def ingest(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source", "-s", help="Source key or kind prefix, e.g. hh, greenhouse:stripe"
        ),
    ] = None,
    all_: Annotated[bool, typer.Option("--all", help="Run every enabled source")] = False,
    extractor: Annotated[str, typer.Option(help="rules | llm")] = "rules",
    storage: Annotated[str | None, typer.Option(help="postgres | memory")] = None,
) -> None:
    """Fetch postings from sources, de-duplicate, extract facts, store."""
    from jobscout.pipeline import Ingestor
    from jobscout.sources import load_sources

    settings = get_settings()
    selected = load_sources(settings)
    if not all_:
        if not source:
            typer.echo("pass --all or at least one --source", err=True)
            raise typer.Exit(2)
        selected = [s for s in selected if any(s.key == k or s.key.startswith(k) for k in source)]
    if not selected:
        typer.echo("no matching sources", err=True)
        raise typer.Exit(1)

    async def run() -> None:
        repo = _repo(settings, storage)
        ext = _extractor(settings, extractor)
        async with make_client(settings) as client:
            report = await Ingestor(repo, ext).run(
                selected, client, stale_after_days=settings.stale_after_days
            )
        for r in report.runs:
            status = f"ERROR {r.error}" if r.error else "ok"
            typer.echo(
                f"{r.source_key:<40} fetched={r.fetched:<5} new={r.new:<5} "
                f"updated={r.updated:<5} {status}"
            )
        typer.echo(
            f"total fetched={report.fetched} new={report.new} closed_stale={report.closed_stale}"
        )
        usage = getattr(ext, "usage", None)
        if usage:
            typer.echo(
                f"llm calls={usage.calls} in_tokens={usage.input_tokens} "
                f"out_tokens={usage.output_tokens} fallbacks={usage.fallbacks}"
            )
        close = getattr(repo, "close", None)
        if close:
            await close()

    asyncio.run(run())


@app.command()
def score(
    profile: Annotated[Path, typer.Option(exists=True, help="Profile YAML")],
    top: int = 10,
    out: Annotated[Path | None, typer.Option(help="Write shortlist + rejected to CSV")] = None,
) -> None:
    """Score all open jobs for a profile; print the top N and why others were rejected."""
    from jobscout.pipeline import score_all
    from jobscout.scoring.profile import CandidateProfile

    settings = get_settings()
    prof = CandidateProfile.load(profile)

    async def run() -> None:
        repo = _repo(settings)
        results = await score_all(repo, prof)
        rows = await repo.list_scores(prof.id, include_rejected=True, limit=len(results) or 1)
        passed = [(j, r) for j, r in rows if not r.gates]
        rejected = [(j, r) for j, r in rows if r.gates]
        typer.echo(
            f"scored {len(results)} open jobs: {len(passed)} pass gates, {len(rejected)} rejected\n"
        )
        typer.echo(f"{'score':>5}  {'age':>4}  {'title':<46} {'company':<22} why")
        for job, res in passed[:top]:
            age = job.age_days()
            typer.echo(
                f"{res.score:>5.1f}  {age if age is not None else '?':>4}  "
                f"{job.title[:46]:<46} {job.company[:22]:<22} {res.why_it_works}"
            )
        reasons = Counter(g.code for _, r in rejected for g in r.gates)
        if reasons:
            typer.echo("\nrejected by: " + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
        if out:
            with out.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(
                    [
                        "score",
                        "passed",
                        "title",
                        "company",
                        "country",
                        "remote",
                        "age_days",
                        "why_it_works",
                        "gaps",
                        "rejected_reasons",
                        "url",
                    ]
                )
                for job, res in rows:
                    w.writerow(
                        [
                            res.score,
                            res.passed,
                            job.title,
                            job.company,
                            job.primary_country or "",
                            job.facts.remote.value,
                            job.age_days(),
                            res.why_it_works,
                            "; ".join(res.gaps),
                            "; ".join(g.reason for g in res.gates),
                            job.apply_url or job.url,
                        ]
                    )
            typer.echo(f"\nwrote {out}")
        close = getattr(repo, "close", None)
        if close:
            await close()

    asyncio.run(run())


@app.command()
def verify(limit: int = 50, profile_id: str | None = None) -> None:
    """Re-check that postings are still open (top matches of a profile, or newest jobs)."""
    from jobscout.liveness import check_many
    from jobscout.storage.base import JobFilter

    settings = get_settings()

    async def run() -> None:
        repo = _repo(settings)
        if profile_id:
            jobs = [
                j
                for j, _ in await repo.list_scores(profile_id, include_rejected=False, limit=limit)
            ]
        else:
            jobs = await repo.list_jobs(JobFilter(limit=limit))
        async with make_client(settings) as client:
            results = await check_many(client, jobs)
        for r in results:
            if r.status.value != "unknown":
                await repo.set_status(r.job_id, r.status, f"verify: {r.reason}")
        counts = Counter(r.status.value for r in results)
        typer.echo(
            f"verified {len(results)}: open={counts['open']} closed={counts['closed']} "
            f"unknown={counts['unknown']}"
        )
        close = getattr(repo, "close", None)
        if close:
            await close()

    asyncio.run(run())


@sources_app.command("list")
def sources_list() -> None:
    from jobscout.sources import load_sources

    for s in load_sources(get_settings()):
        typer.echo(f"{s.key:<45} scope={s.scope.value:<7} directness={s.directness.name}")


@eval_app.command("extraction")
def eval_extraction(
    extractor: Annotated[str, typer.Option(help="rules | llm")] = "rules",
    dataset: Annotated[Path | None, typer.Option()] = None,
    min_accuracy: Annotated[float, typer.Option(help="Fail if micro accuracy is lower")] = 0.0,
    show_misses: bool = False,
    json_out: Annotated[Path | None, typer.Option()] = None,
) -> None:
    from jobscout.evals import DATASET, run_eval

    ext = _extractor(get_settings(), extractor)
    report = asyncio.run(run_eval(ext, dataset or DATASET))
    typer.echo(report.table())
    if show_misses:
        for name, fs in sorted(report.fields.items()):
            for miss in fs.misses:
                typer.echo(f"  [{name}] {miss}")
    if json_out:
        json_out.write_text(
            json.dumps(
                {
                    "extractor": report.extractor,
                    "n": report.n,
                    "micro_accuracy": report.micro_accuracy,
                    "fields": {
                        k: {"accuracy": v.accuracy, "n": v.total} for k, v in report.fields.items()
                    },
                },
                indent=2,
            )
        )
    if report.micro_accuracy < min_accuracy:
        typer.echo(f"FAIL: {report.micro_accuracy:.3f} < {min_accuracy}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    app()
