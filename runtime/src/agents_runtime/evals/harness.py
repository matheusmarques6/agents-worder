"""The eval runner — pack + rubrics + a responder + a judge → a verdict per rubric.

This is the **activation gate** of an agent version, not a CI suite
(`core/testes-e-cicd.md` §5 and §6.1: "por versão de agente (ativação)"). It
never runs on a pull request; it runs when somebody wants to put a version in
front of real customers.

The whole module is pure — no clock, no randomness, no database, no LLM. The
agent under evaluation and the judge arrive as ports, so the runner is proved
against scripted stand-ins before any model exists (plano E2, S3) and the first
real run meets a harness that already works. Persistence is a separate concern
and lives in `repository/evals.py`.

Two rules decide everything here, and both come from D3:

  1. **The floor is per rubric, never an aggregate over the pack.** A single
     number would let a strong rubric pay for a broken one — an agent that is
     excellent at tone and unsafe on prompt injection would activate.
  2. **Zero `critical` is a veto, not a subtraction.** One critical verdict
     blocks its rubric no matter how high the average is.

The rubric's score is the **mean of the per-scenario criteria ratios**, which
is the same unit the rubric's own `threshold` is written in: 0.85 means "85% of
the criteria satisfied", whether inside one scenario or across the pack. The
alternative — counting how many scenarios came out `pass` — would apply the
same 0.85 twice with two different meanings, and would flatten "missed one
criterion" into the same value as "missed all of them".
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from agents_runtime.evals.pack import Scenario
from agents_runtime.evals.rubrics import Rubric, score

# Why a rubric was held back. `None` means it was not.
CRITICAL = "critical"
BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True, eq=False)
class Judgement:
    """What a judge returns about one reply: a verdict per criterion, plus why."""

    verdicts: dict[str, bool]
    rationale: str | None = None


#: The agent under evaluation. In S3 it is scripted; from S11 it is the real
#: responder, which is why the port takes the scenario and returns only text —
#: nothing here may know how the reply was produced.
Responder = Callable[[Scenario], str]


class Judge(Protocol):
    """Judge 1's shape. `model` is part of the port because the evaluation
    trail records which judge produced each score, and a judge that could not
    name itself would leave `judge_scores.judge_model` guessing."""

    model: str

    def __call__(self, scenario: Scenario, reply: str, rubric: Rubric) -> Judgement: ...


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    rubric: str
    outcome: str  # pass | fail | critical
    passed_ratio: float
    rationale: str | None


@dataclass(frozen=True, slots=True)
class RubricAggregate:
    rubric: str
    threshold: float
    scenarios: int
    score: float
    criticals: int
    approved: bool
    blocked_by: str | None


@dataclass(frozen=True, slots=True)
class EvalReport:
    results: tuple[ScenarioResult, ...]
    aggregates: dict[str, RubricAggregate]
    judge_model: str
    approved: bool
    weakest_score: float


def run_pack(
    scenarios: Sequence[Scenario],
    *,
    rubrics: dict[str, Rubric],
    responder: Responder,
    judge: Judge,
) -> EvalReport:
    """Run every scenario and aggregate per rubric. Raises rather than guessing."""
    if not scenarios:
        raise ValueError("empty pack: a run with no scenario proves nothing")
    if not rubrics:
        raise ValueError("no rubric to judge against")

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        rubric = rubrics.get(scenario.rubric)
        if rubric is None:
            raise ValueError(f"{scenario.id}: unknown rubric {scenario.rubric!r}")

        reply = responder(scenario)
        judgement = judge(scenario, reply, rubric)
        # `score` refuses a judgement that does not cover the criteria exactly,
        # and that error is deliberately NOT caught: a half-answered rubric is
        # the judge's failure, and scoring it would blame the agent for it.
        scored = score(rubric, judgement.verdicts)

        results.append(
            ScenarioResult(
                scenario_id=scenario.id,
                rubric=scenario.rubric,
                outcome=scored.outcome,
                passed_ratio=scored.passed_ratio,
                rationale=judgement.rationale,
            )
        )

    exercised = {result.rubric for result in results}
    unexercised = sorted(set(rubrics) - exercised)
    if unexercised:
        # A rubric nobody ran has no evidence, and "approved" without evidence
        # is exactly the false green the whole eval-first order exists to stop.
        raise ValueError(f"rubrics with no scenario in the pack: {unexercised}")

    aggregates = {
        name: _aggregate(rubric, [r for r in results if r.rubric == name])
        for name, rubric in sorted(rubrics.items())
    }

    return EvalReport(
        results=tuple(results),
        aggregates=aggregates,
        judge_model=judge.model,
        approved=all(aggregate.approved for aggregate in aggregates.values()),
        weakest_score=min(aggregate.score for aggregate in aggregates.values()),
    )


def _aggregate(rubric: Rubric, results: list[ScenarioResult]) -> RubricAggregate:
    criticals = sum(1 for result in results if result.outcome == CRITICAL)
    rubric_score = sum(result.passed_ratio for result in results) / len(results)

    if criticals:
        blocked_by = CRITICAL
    elif rubric_score < rubric.threshold:
        blocked_by = BELOW_THRESHOLD
    else:
        blocked_by = None

    return RubricAggregate(
        rubric=rubric.name,
        threshold=rubric.threshold,
        scenarios=len(results),
        score=rubric_score,
        criticals=criticals,
        approved=blocked_by is None,
        blocked_by=blocked_by,
    )
