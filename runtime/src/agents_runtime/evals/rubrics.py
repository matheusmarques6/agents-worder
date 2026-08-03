"""Rubric parsing and scoring — pure, no I/O, no LLM.

The judge (S3/S8) produces per-criterion verdicts; this module turns them
into `pass | fail | critical` against the rubric's own floor (D3 is per
rubric, never an aggregate). One failed critical criterion is a VETO — the
outcome is `critical` whatever the ratio says. Parsing is strict for the same
reason webhook schemas are: a half-read rubric would approve an agent against
a contract nobody wrote.
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
    return ValueError(f"rubrica inválida: {reason}")


def parse_rubric(raw: dict) -> Rubric:
    unexpected = set(raw) - _RUBRIC_FIELDS
    if unexpected:
        raise _reject(f"campos inesperados {sorted(unexpected)}")
    missing = _RUBRIC_FIELDS - set(raw)
    if missing:
        raise _reject(f"campos ausentes {sorted(missing)}")

    name, version = raw["name"], raw["version"]
    if not isinstance(name, str) or not name:
        raise _reject("name deve ser texto não-vazio")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _reject("version deve ser inteiro >= 1")

    rfs = raw["rfs"]
    # Rastreabilidade é regra do projeto: uma rubrica sem RF valida coisa
    # nenhuma — ela não tem o direito de reprovar um agente.
    if not isinstance(rfs, list) or not rfs:
        raise _reject("rfs deve listar ao menos um requisito")
    for rf in rfs:
        if not isinstance(rf, str) or not RF_PATTERN.match(rf):
            raise _reject(f"rf fora do padrão RF-xxx: {rf!r}")

    threshold = raw["threshold"]
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise _reject("threshold deve ser numérico")
    if not 0 <= threshold <= 1:
        raise _reject(f"threshold fora de [0, 1]: {threshold}")

    raw_criteria = raw["criteria"]
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise _reject("criteria deve listar ao menos um critério")

    criteria = []
    for entry in raw_criteria:
        if not isinstance(entry, dict) or set(entry) != _CRITERION_FIELDS:
            raise _reject(f"critério com forma errada: {entry!r}")
        if entry["severity"] not in SEVERITIES:
            raise _reject(f"severidade desconhecida: {entry['severity']!r}")
        if not isinstance(entry["id"], str) or not entry["id"]:
            raise _reject("critério sem id")
        criteria.append(
            Criterion(
                id=entry["id"],
                severity=entry["severity"],
                description=str(entry["description"]),
            )
        )

    ids = [criterion.id for criterion in criteria]
    if len(ids) != len(set(ids)):
        raise _reject("ids de critério duplicados")

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
            "vereditos não cobrem exatamente os critérios: "
            f"faltam {sorted(expected - set(verdicts))}, "
            f"sobram {sorted(set(verdicts) - expected)}"
        )

    failed_critical = any(
        criterion.severity == "critical" and not verdicts[criterion.id]
        for criterion in rubric.criteria
    )
    passed_ratio = sum(verdicts.values()) / len(rubric.criteria)

    if failed_critical:
        # Veto, não nota: zero critical é inegociável (D3), e um veto com
        # razão alta continua sendo veto.
        return ScoreResult(outcome="critical", passed_ratio=passed_ratio)
    if passed_ratio < rubric.threshold:
        return ScoreResult(outcome="fail", passed_ratio=passed_ratio)
    return ScoreResult(outcome="pass", passed_ratio=passed_ratio)
