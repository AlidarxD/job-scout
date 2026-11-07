"""Field-extraction eval: per-field accuracy of an extractor against a labelled JSONL set.

Each line: {"posting": RawPosting, "expected": {<subset of JobFacts fields>}}
Only fields present in "expected" are graded, so labels can be partial. Lists are graded as
sets; salary is graded on currency + period + min/max within 5%.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobscout.domain import JobFacts, RawPosting
from jobscout.extract import Extractor

DATASET = Path("evals/extraction/dataset.jsonl")


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class EvalReport:
    extractor: str
    n: int
    fields: dict[str, FieldScore]

    @property
    def micro_accuracy(self) -> float:
        correct = sum(f.correct for f in self.fields.values())
        total = sum(f.total for f in self.fields.values())
        return correct / total if total else 0.0

    def table(self) -> str:
        lines = [
            f"extractor={self.extractor} postings={self.n} micro_acc={self.micro_accuracy:.3f}",
            f"{'field':<22}{'acc':>7}{'n':>6}",
        ]
        for name, fs in sorted(self.fields.items()):
            lines.append(f"{name:<22}{fs.accuracy:>7.2f}{fs.total:>6}")
        return "\n".join(lines)


def load_dataset(path: Path = DATASET) -> list[tuple[RawPosting, dict[str, Any]]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            rows.append((RawPosting.model_validate(obj["posting"]), obj["expected"]))
    return rows


def _norm(name: str, value: Any) -> Any:
    if isinstance(value, list):
        return sorted({str(v).lower() for v in value})
    if name == "seniority" and isinstance(value, int | str):
        from jobscout.domain import Seniority

        return Seniority[value.upper()].value if isinstance(value, str) else int(value)
    return value


def field_correct(name: str, expected: Any, got: JobFacts) -> bool:
    actual = getattr(got, name)
    if name == "salary":
        if expected is None or actual is None:
            return expected is None and actual is None
        if (
            expected.get("currency") != actual.currency
            or expected.get("period", "year") != actual.period
        ):
            return False
        for key in ("min", "max"):
            exp, act = expected.get(key), getattr(actual, key)
            if exp is None and act is None:
                continue
            if exp is None or act is None or abs(exp - act) > 0.05 * exp:
                return False
        return True
    if name == "skills":  # recall of labelled skills; extra skills are not penalised
        return set(_norm(name, expected)) <= set(_norm(name, actual))
    if hasattr(actual, "value"):
        actual = actual.value
    return bool(_norm(name, expected) == _norm(name, actual))


async def run_eval(extractor: Extractor, path: Path = DATASET) -> EvalReport:
    data = load_dataset(path)
    fields: dict[str, FieldScore] = defaultdict(FieldScore)
    for posting, expected in data:
        facts = await extractor.extract(posting)
        for name, exp in expected.items():
            fs = fields[name]
            fs.total += 1
            if field_correct(name, exp, facts):
                fs.correct += 1
            else:
                fs.misses.append(
                    f"{posting.source}:{posting.external_id} expected={exp!r} "
                    f"got={getattr(facts, name)!r}"
                )
    return EvalReport(extractor=extractor.name, n=len(data), fields=dict(fields))
