"""Builds the seed eval set (synthetic postings modelled on real ones, hand-labelled).

Re-run after editing: python evals/extraction/build_seed.py
Real crawled postings labelled via `jobscout eval label` (planned) are appended to the same file.
"""

import json
from pathlib import Path

P = lambda i, title, company, desc, **kw: {  # noqa: E731
    "posting": {
        "source": "seed",
        "external_id": str(i),
        "url": f"https://example.com/{i}",
        "title": title,
        "company": company,
        "description": desc,
        **kw,
    },
}
ROWS = [
    (
        P(
            1,
            "Senior Backend Engineer",
            "Acme",
            "5+ years with Python. We can sponsor visas.",
            location_raw="Berlin, Germany",
        ),
        {
            "seniority": "senior",
            "years_min": 5,
            "visa_sponsorship": True,
            "countries": ["DE"],
            "citizenship_required": [],
            "required_languages": [],
        },
    ),
    (
        P(
            2,
            "Software Engineer",
            "Globex",
            "Requires 7+ years building distributed systems in Go.",
            location_raw="Amsterdam",
        ),
        {"seniority": "senior", "years_min": 7, "countries": ["NL"]},
    ),
    (
        P(
            3,
            "Data Analyst",
            "Riyadh Holding",
            "This position is open to Saudi nationals only in line with Saudization.",
            location_raw="Riyadh, Saudi Arabia",
        ),
        {"citizenship_required": ["SA"], "countries": ["SA"]},
    ),
    (
        P(
            4,
            "Product Engineer",
            "Emirates Tech",
            "As part of our Emiratisation program, this role is for UAE nationals only.",
            location_raw="Dubai, UAE",
        ),
        {"citizenship_required": ["AE"], "countries": ["AE"]},
    ),
    (
        P(
            5,
            "ML Engineer",
            "Tokyo AI",
            "Business-level Japanese (JLPT N2) is required. PyTorch.",
            location_raw="Tokyo, Japan",
        ),
        {"required_languages": ["ja"], "countries": ["JP"], "skills": ["pytorch"]},
    ),
    (
        P(
            6,
            "Backend Engineer",
            "Tokyo SaaS",
            "English-only team. Japanese is a plus but not required.",
            location_raw="Tokyo",
        ),
        {"required_languages": [], "countries": ["JP"]},
    ),
    (
        P(
            7,
            "Platform Engineer",
            "Remote Co",
            "Fully remote, but you must be based in the United States.",
            location_raw="Remote",
            remote_hint="remote",
        ),
        {"remote": "remote", "remote_regions": ["US"]},
    ),
    (
        P(
            8,
            "Senior Python Developer",
            "Worldwide Inc",
            "Work from anywhere. Python, FastAPI, PostgreSQL.",
            location_raw="Remote - Anywhere",
            remote_hint="remote",
        ),
        {
            "remote": "remote",
            "remote_regions": ["WORLD"],
            "seniority": "senior",
            "skills": ["python", "fastapi", "postgresql"],
        },
    ),
    (
        P(
            9,
            "Site Reliability Engineer",
            "EMEA Corp",
            "Kubernetes, Terraform, AWS.",
            location_raw="Remote (EMEA)",
            remote_hint="remote",
        ),
        {"remote_regions": ["EMEA"], "skills": ["kubernetes", "terraform", "aws"]},
    ),
    (
        P(
            10,
            "Junior Data Scientist",
            "Halyk",
            "Опыт работы от 1 года. Python, SQL. Знание казахского языка обязательно.",
            location_raw="Астана, Kazakhstan",
        ),
        {"seniority": "junior", "years_min": 1, "required_languages": ["kk"], "countries": ["KZ"]},
    ),
    (
        P(
            11,
            "Python разработчик",
            "Kolesa",
            "Опыт коммерческой разработки от 3 лет. Django, Redis. Зарплата: от 900 000 ₸",
            location_raw="Алматы",
        ),
        {
            "years_min": 3,
            "countries": ["KZ"],
            "skills": ["python", "django", "redis"],
            "salary": {"min": 900000, "max": None, "currency": "KZT", "period": "month"},
        },
    ),
    (
        P(
            12,
            "Team Lead Backend",
            "Chocofamily",
            "Руководство командой из 6 человек. Go, Kafka.",
            location_raw="Алматы, гибрид",
        ),
        {"seniority": "lead", "remote": "hybrid", "countries": ["KZ"], "skills": ["go", "kafka"]},
    ),
    (
        P(
            13,
            "Staff Engineer, Infrastructure",
            "Stripe-like",
            "Own the architecture of our core ledger.",
            location_raw="Dublin, Ireland",
        ),
        {"seniority": "lead", "countries": ["IE"]},
    ),
    (
        P(
            14,
            "Software Engineer Intern",
            "BigCo",
            "Summer internship for students.",
            location_raw="London, UK",
        ),
        {"seniority": "intern", "countries": ["GB"]},
    ),
    (
        P(
            15,
            "Backend Engineer",
            "No Sponsor Ltd",
            "Applicants must have the right to work in the UK; we are unable to sponsor visas.",
            location_raw="London",
        ),
        {"visa_sponsorship": False, "countries": ["GB"]},
    ),
    (
        P(
            16,
            "AI Engineer",
            "Relo GmbH",
            "We offer a relocation package and visa support for international hires. LLM, RAG, LangGraph.",
            location_raw="Munich, Germany",
        ),
        {
            "relocation": True,
            "visa_sponsorship": True,
            "countries": ["DE"],
            "skills": ["llm", "rag", "langgraph"],
        },
    ),
    (
        P(
            17,
            "Defense Software Engineer",
            "Clearance Inc",
            "Must be a U.S. citizen with an active TS/SCI security clearance.",
            location_raw="Arlington, VA, USA",
        ),
        {"citizenship_required": ["US"], "countries": ["US"]},
    ),
    (
        P(
            18,
            "Data Engineer",
            "Euro Data",
            "Remote within the European Union. Spark, Airflow, dbt.",
            location_raw="Remote (EU)",
            remote_hint="remote",
        ),
        {"remote": "remote", "remote_regions": ["EU"], "skills": ["spark", "airflow", "dbt"]},
    ),
    (
        P(
            19,
            "Middle Frontend Developer",
            "Kaspi",
            "React, TypeScript. 2+ years.",
            location_raw="Almaty",
        ),
        {
            "seniority": "mid",
            "years_min": 2,
            "skills": ["react", "typescript"],
            "countries": ["KZ"],
        },
    ),
    (
        P(
            20,
            "Senior ML Engineer",
            "Gulf AI",
            "Salary: AED 35,000 - 45,000 per month. Python, PyTorch.",
            location_raw="Abu Dhabi, UAE",
        ),
        {
            "seniority": "senior",
            "countries": ["AE"],
            "salary": {"min": 35000, "max": 45000, "currency": "AED", "period": "month"},
        },
    ),
    (
        P(
            21,
            "Backend Engineer",
            "US Startup",
            "Base salary $140k–$180k. Go, PostgreSQL.",
            location_raw="San Francisco, CA",
        ),
        {
            "countries": ["US"],
            "salary": {"min": 140000, "max": 180000, "currency": "USD", "period": "year"},
        },
    ),
    (
        P(
            22,
            "ML Engineer (m/w/d)",
            "Berlin AI",
            "Fluent German (C1) required. Python.",
            location_raw="Berlin",
        ),
        {"required_languages": ["de"], "countries": ["DE"]},
    ),
    (
        P(
            23,
            "Software Engineer",
            "Hybrid Co",
            "Hybrid: 3 days in our Warsaw office. Java, Kafka.",
            location_raw="Warsaw, Poland",
        ),
        {"remote": "hybrid", "countries": ["PL"], "skills": ["java", "kafka"]},
    ),
    (
        P(
            24,
            "Principal Engineer",
            "Scale Corp",
            "10+ years of experience.",
            location_raw="Singapore",
        ),
        {"seniority": "principal", "years_min": 10, "countries": ["SG"]},
    ),
    (
        P(
            25,
            "Forward Deployed Engineer",
            "Agents Inc",
            "Remote for candidates in North America only. Python, LLM, MCP.",
            location_raw="Remote",
            remote_hint="remote",
        ),
        {"remote": "remote", "remote_regions": ["AMERICAS"], "skills": ["python", "llm", "mcp"]},
    ),
    (
        P(
            26,
            "DevOps Engineer",
            "Qatar Energy Digital",
            "Qatari nationals preferred under the Qatarization program.",
            location_raw="Doha, Qatar",
        ),
        {"countries": ["QA"], "citizenship_required": []},
    ),
    (
        P(
            27,
            "Software Engineer",
            "Plain Co",
            "Build features with our team.",
            location_raw="Toronto, Canada",
        ),
        {
            "seniority": "unknown",
            "years_min": None,
            "visa_sponsorship": None,
            "relocation": None,
            "countries": ["CA"],
            "required_languages": [],
        },
    ),
    (
        P(
            28,
            "Senior Data Engineer",
            "Remote Global",
            "We hire globally. Spark, Kafka, ClickHouse. No relocation.",
            location_raw="Remote - Worldwide",
            remote_hint="remote",
        ),
        {
            "remote_regions": ["WORLD"],
            "relocation": False,
            "skills": ["spark", "kafka", "clickhouse"],
        },
    ),
    (
        P(
            29,
            "Junior Python Developer",
            "Astana Hub startup",
            "Стажировка не требуется, опыт от 1 года.",
            location_raw="Астана",
        ),
        {"seniority": "junior", "years_min": 1, "countries": ["KZ"]},
    ),
    (
        P(
            30,
            "Backend Engineer",
            "Tbilisi Tech",
            "Relocation to Tbilisi, Georgia with relocation assistance.",
            location_raw="Tbilisi",
        ),
        {"relocation": True, "countries": ["GE"]},
    ),
]

out = Path(__file__).with_name("dataset.jsonl")
with out.open("w", encoding="utf-8") as fh:
    for posting, expected in ROWS:
        fh.write(json.dumps({**posting, "expected": expected}, ensure_ascii=False) + "\n")
print(f"wrote {len(ROWS)} rows to {out}")
