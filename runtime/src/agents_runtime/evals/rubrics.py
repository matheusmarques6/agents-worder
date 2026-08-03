"""Rubric parsing and scoring — pure, no I/O, no LLM.

The judge (S3/S8) produces per-criterion verdicts; this module turns them
into `pass | fail | critical` against the rubric's own floor (D3 is per
rubric, never an aggregate). One failed critical criterion is a VETO — the
outcome is `critical` whatever the ratio says. Parsing is strict for the same
reason webhook schemas are: a half-read rubric would approve an agent against
a contract nobody wrote.

Criterion descriptions stay in PT-BR: they are the contract the judge reads
about PT-BR conversations, so they are content, not code.
"""

import re
from dataclasses import dataclass

RF_PATTERN = re.compile(r"^RF-\d{3}$")

SEVERITIES = ("standard", "critical")

_RUBRIC_FIELDS = {"name", "version", "rfs", "threshold", "criteria"}
_CRITERION_FIELDS = {"id", "severity", "description"}


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    severity: str
    description: str


@dataclass(frozen=True, slots=True)
class Rubric:
    name: str
    version: int
    rfs: tuple[str, ...]
    threshold: float
    criteria: tuple[Criterion, ...]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    outcome: str  # pass | fail | critical
    passed_ratio: float


def _reject(reason: str) -> ValueError:
    return ValueError(f"invalid rubric: {reason}")


def parse_rubric(raw: dict) -> Rubric:
    unexpected = set(raw) - _RUBRIC_FIELDS
    if unexpected:
        raise _reject(f"unexpected fields {sorted(unexpected)}")
    missing = _RUBRIC_FIELDS - set(raw)
    if missing:
        raise _reject(f"missing fields {sorted(missing)}")

    name, version = raw["name"], raw["version"]
    if not isinstance(name, str) or not name:
        raise _reject("name must be non-empty text")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _reject("version must be an integer >= 1")

    rfs = raw["rfs"]
    # Traceability is a project rule: a rubric with no RF validates nothing —
    # it has no right to fail an agent.
    if not isinstance(rfs, list) or not rfs:
        raise _reject("rfs must list at least one requirement")
    for rf in rfs:
        if not isinstance(rf, str) or not RF_PATTERN.match(rf):
            raise _reject(f"rf outside the RF-xxx pattern: {rf!r}")

    threshold = raw["threshold"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise _reject("threshold must be numeric")
    if not 0 <= threshold <= 1:
        raise _reject(f"threshold outside [0, 1]: {threshold}")

    raw_criteria = raw["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise _reject("criteria must list at least one criterion")

    criteria = []
    for entry in raw_criteria:
        if not isinstance(entry, dict) or set(entry) != _CRITERION_FIELDS:
            raise _reject(f"criterion with the wrong shape: {entry!r}")
        if entry["severity"] not in SEVERITIES:
            raise _reject(f"unknown severity: {entry['severity']!r}")
        if not isinstance(entry["id"], str) or not entry["id"]:
            raise _reject("criterion without an id")
        # Never `str(...)`: coercing would turn None into "None" and a list
        # into its repr, and the judge would read a contract nobody wrote.
        if not isinstance(entry["description"], str) or not entry["description"]:
            raise _reject(
                f"criterion {entry['id']!r}: description must be non-empty text"
            )
        criteria.append(
            Criterion(
                id=entry["id"],
                severity=entry["severity"],
                description=entry["description"],
            )
        )

    ids = [criterion.id for criterion in criteria]
    if len(ids) != len(set(ids)):
        raise _reject("duplicate criterion ids")

    return Rubric(
        name=name,
        version=version,
        rfs=tuple(rfs),
        threshold=float(threshold),
        criteria=tuple(criteria),
    )


def score(rubric: Rubric, verdicts: dict[str, bool]) -> ScoreResult:
    """Per-criterion verdicts → the rubric's outcome.

    A partial judgement is not a judgement: the verdict keys must cover the
    rubric's criteria exactly, or the error is the judge's, not the agent's.
    """
    expected = {criterion.id for criterion in rubric.criteria}
    if set(verdicts) != expected:
        raise ValueError(
            "verdicts do not cover exactly the criteria: "
            f"missing {sorted(expected - set(verdicts))}, "
            f"extra {sorted(set(verdicts) - expected)}"
        )

    failed_critical = any(
        criterion.severity == "critical" and not verdicts[criterion.id]
        for criterion in rubric.criteria
    )
    passed_ratio = sum(verdicts.values()) / len(rubric.criteria)

    if failed_critical:
        # A veto, not a score: zero critical is non-negotiable (D3), and a
        # veto with a high ratio is still a veto.
        return ScoreResult(outcome="critical", passed_ratio=passed_ratio)
    if passed_ratio < rubric.threshold:
        return ScoreResult(outcome="fail", passed_ratio=passed_ratio)
    return ScoreResult(outcome="pass", passed_ratio=passed_ratio)
