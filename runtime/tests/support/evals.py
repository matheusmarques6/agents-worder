"""Stand-ins for the eval harness — the scripted responder and the scripted judge.

The harness is born against these (plano E2, S3): the whole point of the step
is that the runner, the aggregation and the persistence are proved BEFORE any
LLM exists, so the first real model to run the pack meets a harness that
already works.

Both stand-ins are deterministic and answer from a table written by the test.
Neither is a mock: they are real implementations of the harness's two ports,
so the assertions are about the harness's behaviour and never about "was this
called".
"""

from agents_runtime.evals.harness import Judgement
from agents_runtime.evals.pack import Scenario, parse_scenario
from agents_runtime.evals.rubrics import Rubric, parse_rubric


def a_rubric(
    name: str,
    *,
    standard: int,
    critical: int = 0,
    threshold: float = 0.85,
) -> dict:
    """A rubric shaped like the real ones, with the criteria count the test needs."""
    criteria = [
        {"id": f"{name}-s{i}", "severity": "standard", "description": f"critério padrão {i}"}
        for i in range(1, standard + 1)
    ]
    criteria += [
        {"id": f"{name}-c{i}", "severity": "critical", "description": f"critério crítico {i}"}
        for i in range(1, critical + 1)
    ]
    return {
        "name": name,
        "version": 1,
        "rfs": ["RF-010"],
        "threshold": threshold,
        "criteria": criteria,
    }


def a_scenario(scenario_id: str, rubric: str) -> dict:
    return {
        "id": scenario_id,
        "rubric": rubric,
        "rfs": ["RF-010"],
        "occasion": "direct",
        "messages": [{"author": "contact", "text": "vocês entregam em Manaus?"}],
        "expectation": "Responde sobre a entrega sem inventar prazo.",
    }


def rubrics_of(*raw: dict) -> dict[str, Rubric]:
    return {entry["name"]: parse_rubric(entry) for entry in raw}


def scenarios_of(*raw: dict) -> tuple[Scenario, ...]:
    return tuple(parse_scenario(entry) for entry in raw)


class ScriptedResponder:
    """The agent under evaluation, scripted: scenario id → the reply it gives."""

    def __init__(self, replies: dict[str, str], *, default: str = "resposta padrão"):
        self._replies = replies
        self._default = default
        self.seen: list[str] = []

    def __call__(self, scenario: Scenario) -> str:
        self.seen.append(scenario.id)
        return self._replies.get(scenario.id, self._default)


class ScriptedJudge:
    """Judge 1's stand-in: scenario id → which criteria it considers satisfied.

    `model` is part of the port because the evaluation trail records which
    judge produced a score. A stand-in that labelled itself `claude-haiku-4-5`
    would put a lie in `judge_scores.judge_model`, so it says what it is.
    """

    model = "scripted-stand-in"

    def __init__(self, verdicts: dict[str, dict[str, bool]], *, default: bool = True):
        self._verdicts = verdicts
        self._default = default
        self.replies_seen: dict[str, str] = {}

    def __call__(self, scenario: Scenario, reply: str, rubric: Rubric) -> Judgement:
        self.replies_seen[scenario.id] = reply
        scripted = self._verdicts.get(scenario.id)
        if scripted is None:
            scripted = {criterion.id: self._default for criterion in rubric.criteria}
        return Judgement(
            verdicts=dict(scripted),
            rationale=f"dublê roteirizado sobre {scenario.id}",
        )
