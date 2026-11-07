# Job Scout

Job Scout pulls vacancies from **Kazakhstan job boards** and from **employer and remote-job feeds worldwide**. It merges duplicates across sources and extracts the facts that decide whether you can actually apply: citizenship rules, required languages, remote country limits, visa sponsorship and real seniority. Each vacancy is scored 0–10 against a saved candidate profile. Every rejected vacancy comes with a written reason.

```
                ┌────────────── sources ──────────────┐
 Kazakhstan     │ hh.kz API · Telegram channels · HTML │──┐
 Global         │ Greenhouse · Lever · Ashby · Workable│  │   map to RawPosting
                │ SmartRecruiters · Remotive · RemoteOK│  ├─► dedup / merge ──► extract facts ──► Postgres
                │ Himalayas · Arbeitnow · HN · LinkedIn│  │   (fingerprint,     (rules | LLM +   (pgvector,
                └──────────────────────────────────────┘──┘    most-direct URL)  Pydantic)        tsvector)
                                                                                                 │
                     web board  ◄── FastAPI ◄── gates + 0-10 rubric per profile ◄────────────────┘
                     CLI: ingest · score · verify · eval
```

## Why this exists

General job boards are noisy in exactly the ways that cost an international candidate time:

| Problem | What Job Scout does |
|---|---|
| The same role appears on LinkedIn, a remote board and the employer's own ATS | Fingerprint dedup (company + title + place). The **most direct** URL is kept, so you never get an aggregator-only link when the employer page exists |
| A role that is "remote" turns out to be remote **for US residents only** | `remote_regions` is extracted; a profile-aware gate rejects it with the reason "remote only for US" |
| A role is for nationals only (Saudization, Emiratisation) or needs a US clearance | `citizenship_required` gate |
| Japanese N2 or fluent German is required | `required_languages` gate. "Nice to have" mentions are ignored |
| A role is titled Mid but asks for 6+ years | The level is raised to senior from the stated years of experience |
| The posting is already closed | ATS boards list every open role, so a role that disappears from its board is marked closed. `jobscout verify` re-checks the links on a shortlist |
| Salaries come in ₸/month, $/year, ¥/year | Converted to USD per month (approximate fixed FX table, see `normalize/salary.py`) |

## Sources

| Scope | Source | How | Directness |
|---|---|---|---|
| KZ | **hh.kz** | official `api.hh.ru` (`area=40`, `host=hh.kz`), vacancy cards for full text + key skills | board |
| KZ | **Telegram channels** | public preview `t.me/s/<channel>`, vacancy-post detection, field parsing (Компания/Город/Зарплата) | board |
| KZ | **dsml.kz** and other small boards | config-driven CSS selectors, one listing request per run | board |
| Global | **Greenhouse, Lever, Ashby, Workable, SmartRecruiters** | public job-board APIs of the employer's ATS (Anthropic, OpenAI, Stripe, Databricks…) | employer |
| Global | **Remotive, RemoteOK, Himalayas, Arbeitnow** | public JSON feeds, links back as their terms ask | aggregator |
| Global | **HN "Who is hiring"** | Algolia HN API, pipe-header parsing | board |
| Global | **LinkedIn** | via an Apify actor (we never scrape LinkedIn directly); enabled only when `JOBSCOUT_APIFY_TOKEN` is set | aggregator |

Adding a company means adding one line to `config/sources.yaml` (`{ name: X, greenhouse: x }`). Adding a source type means adding a class with `fetch()` plus a pure `parse_*()` function that has fixture tests.

## Scoring

1. **Gates.** These are deal-breakers: closed, blacklisted, citizenship, language, remote region, geography, visa, seniority out of range, salary below floor. Gated jobs go to a *rejected with reasons* list. They are never dropped silently.
2. **Rubric.** Six dimensions scored 0–10, with fixed weights: role 25%, skills 25%, seniority 15%, location 15%, domain 10%, compensation 10%. Calibration is strict: an unknown dimension scores a neutral 5, never a hopeful 8. Each result explains itself: `strengths`, `gaps`, `why_it_works` (for example "remote worldwide" or "visa sponsorship + relocation support").

## Extraction and evals

- `RuleExtractor` is deterministic, works offline, and serves as the fallback.
- `LlmExtractor` (Anthropic):
  - The model is forced to call one tool whose input schema is the Pydantic model, then the output is validated.
  - If validation fails, the error goes back to the model for one repair attempt; if that also fails, the rule extractor is used instead.
  - Structured source data (hh experience, key skills, ATS salary) takes priority over model output.
  - Token usage is tracked per run.
- `jobscout eval extraction --extractor rules|llm` reports per-field accuracy on `evals/extraction/dataset.jsonl`. CI fails if the rule baseline regresses.

> **Honesty note.** The current eval set has 30 *synthetic* postings, modelled on real ones and labelled by hand. They were written alongside the rule extractor, so the rule baseline's ~100% on this set is a regression guard, **not** a quality claim. The next milestone is ≥300 real crawled postings, labelled and held out, with rules vs LLM compared on accuracy, cost per 1k postings and latency.

## Run it

```bash
cp .env.example .env              # fill in only what you use; never commit .env
docker compose up -d --build api  # Postgres+pgvector, Redis, migrations, API on :8000
docker compose run --rm ingest    # crawl every enabled source
open http://localhost:8000        # board with KZ / Global tabs and filters
```

Local development:

```bash
make install          # uv venv + deps
make test             # unit tests (fixtures, no network)
make test-int         # Postgres integration tests (JOBSCOUT_TEST_DATABASE_URL)
make lint typecheck

jobscout ingest -s hh -s telegram --storage memory -v       # quick crawl without a DB
jobscout score --profile profiles/me.yaml --top 15 --out shortlist.csv
jobscout verify --profile-id me --limit 30                  # re-check links are still open
```

The profile format is shown in `config/profile.example.yaml`.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/jobs?scope=local\|global&country=KZ&remote=remote&seniority=senior&q=&days=7` | `country` means "open to a person in this country": an office there, or remote work allowed there |
| GET | `/api/jobs/{id}` | full description and facts |
| GET | `/api/stats` | open jobs by scope, country and source, plus recent crawl runs and their errors |
| PUT/GET | `/api/profiles/{id}` | saved candidate profile |
| POST | `/api/profiles/{id}/score` | re-score all open jobs |
| GET | `/api/profiles/{id}/matches?include_rejected=true` | shortlist, plus rejections with reasons |
| PUT/GET | `/api/profiles/{id}/applications/{job_id}` | pipeline: saved → applied → interviewing → offer / rejected / withdrawn |

## Roadmap

- [ ] Label 300+ real postings and publish the rules-vs-LLM eval (accuracy, $/1k, p95 latency)
- [ ] Telegram bot: daily matches above threshold, `/applied` to update the pipeline
- [ ] Private Telegram channels through an MTProto user session (`telegram_private`)
- [ ] Embeddings (pgvector) for semantic search, CV ↔ requirement evidence matching
- [ ] CV tailoring per vacancy that never invents experience and states gaps openly
- [ ] Learning-to-rank from clicks and applications (NDCG offline, A/B online)
- [ ] Gmail recruiter-message ingestion
- [ ] Scheduled crawls (k8s CronJob), OpenTelemetry traces, per-source health alerts

## Credits

Ideas taken from two Claude skills:
- [gregorymm/job-scout](https://github.com/gregorymm/job-scout): saved profile, strict 0–10 rubric, LinkedIn through Apify, CV tailoring that never fabricates.
- [aladac's job-scout](https://mcpmarket.com/tools/skills/job-scout): pipeline tracking, watchlist and blacklist, HN Who's Hiring.

Job Scout turns those ideas into a service with storage, dedup, evals and an API.
