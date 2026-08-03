"""Judge 1 — the gate every reply passes before it leaves (RF-015).

The rubrics are the ones from S1 (`runtime/evals/rubrics/`), so the gate that
blocks a send and the gate that activates a version speak the same criteria.
What differs is what a failure MEANS, and that was fixed with Bruno on
2026-08-03:

  * a failed `critical` criterion → **nothing goes out**, and no regeneration:
    the second attempt at a security violation is another security violation,
    and it would cost a model call to find out;
  * only `standard` criteria failed → regenerate, telling the generator which
    ones failed (regenerating without saying what was wrong is repeating);
  * regenerations exhausted with only standard failures → **send the best
    version**. A customer never waits in silence because the agent could not
    get the tone right in three tries;
  * a judge whose answer cannot be read counts as a failure (fail-closed), and
    a draft that no attempt could judge never goes out — "unjudged" must never
    become "approved" by exhaustion.

The per-message gate deliberately does NOT apply the 0.85 floor of D3: that
floor belongs to the activation gate, over a whole pack (S3/S11). Applying the
same number per message would be one number with two meanings — the mistake
decisão 82(a) exists to prevent.

The judge's model is a platform constant. A safety gate a customer can
reconfigure is not a gate (D1), and `tests/unit/test_pre_send_judge.py` fails
the build if this module ever reads a model off a configuration object.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from agents_runtime.agent_core.llm import ChatRequest, LlmPort, Message
from agents_runtime.evals.rubrics import Rubric, score

#: Platform-fixed (D1, decisão 79). Never per tenant.
JUDGE_MODEL = "claude-haiku-4-5"

#: Canonical default (CLAUDE.md): two regenerations, then the decision above.
REGENERATION_LIMIT = 2

PASS, FAIL, CRITICAL = "pass", "fail", "critical"

# Worst wins: one critical anywhere blocks, whatever the rest looks like.
_SEVERITY = {PASS: 0, FAIL: 1, CRITICAL: 2}


class JudgeError(Exception):
    """The judge did not produce a judgement. Never means "approved"."""


@dataclass(frozen=True, slots=True)
class RubricVerdict:
    rubric: str
    outcome: str
    passed_ratio: float
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Judgement:
    outcome: str
    score: float
    rubrics: tuple[RubricVerdict, ...]
    rationale: str | None = None

    @property
    def usable(self) -> bool:
        """A judgement with no rubric behind it is a placeholder for a judge
        that failed — it may be counted as a refusal, never as evidence."""
        return bool(self.rubrics)

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        return tuple(criterion for verdict in self.rubrics for criterion in verdict.failed)


@dataclass(frozen=True, slots=True)
class JudgeContext:
    """What the judge needs to tell a good reply from a plausible one."""

    conversation: tuple[str, ...]
    knowledge: tuple[str, ...] = ()
    language: str = "pt-BR"
    never_say_ai: bool = True


@dataclass(frozen=True, slots=True)
class GuardedOutcome:
    #: None means nothing goes out.
    draft: str | None
    judgement: Judgement | None
    attempts: int
    blocked_by: str | None = None
    judgements: tuple[Judgement, ...] = field(default_factory=tuple)


def judge_verdicts(
    rubrics: Mapping[str, Rubric],
    verdicts: Mapping[str, bool],
    *,
    rationale: str | None = None,
) -> Judgement:
    """Per-criterion verdicts → one judgement, scored rubric by rubric.

    Strict in both directions: a criterion left unjudged and a criterion nobody
    wrote are both the judge failing its contract, and neither may be quietly
    absorbed into a score.
    """
    known: set[str] = set()
    results: list[RubricVerdict] = []

    for name, rubric in sorted(rubrics.items()):
        subset: dict[str, bool] = {}
        for criterion in rubric.criteria:
            if criterion.id not in verdicts:
                raise JudgeError(f"criterion left unjudged: {criterion.id}")
            value = verdicts[criterion.id]
            if not isinstance(value, bool):
                raise JudgeError(f"verdict for {criterion.id} is not a boolean: {value!r}")
            subset[criterion.id] = value
        known.update(subset)

        scored = score(rubric, subset)
        results.append(
            RubricVerdict(
                rubric=name,
                outcome=scored.outcome,
                passed_ratio=scored.passed_ratio,
                failed=tuple(cid for cid, ok in subset.items() if not ok),
            )
        )

    invented = sorted(set(verdicts) - known)
    if invented:
        raise JudgeError(f"verdicts for criteria nobody wrote: {invented}")

    outcome = max((verdict.outcome for verdict in results), key=lambda o: _SEVERITY[o])
    return Judgement(
        outcome=outcome,
        score=min(verdict.passed_ratio for verdict in results),
        rubrics=tuple(results),
        rationale=rationale,
    )


class PreSendJudge:
    """The judge that calls a model. Wrap it in `MeteredLlm(purpose="judge_pre")`
    at composition and the gate's cost shows up separately from the agent's."""

    def __init__(self, llm: LlmPort, rubrics: Mapping[str, Rubric]) -> None:
        self._llm = llm
        self._rubrics = dict(rubrics)

    async def __call__(self, draft: str, context: JudgeContext) -> Judgement:
        request = ChatRequest(
            model=JUDGE_MODEL,
            messages=(
                Message(role="system", content=self._instructions()),
                Message(role="user", content=self._case(draft, context)),
            ),
        )
        answer = await self._llm.chat(request)

        try:
            parsed = json.loads(answer.text)
        except (TypeError, ValueError) as error:
            raise JudgeError(f"judge answer is not JSON: {error}") from error

        if not isinstance(parsed, dict) or not isinstance(parsed.get("verdicts"), dict):
            raise JudgeError("judge answer has no verdicts object")

        rationale = parsed.get("rationale")
        return judge_verdicts(
            self._rubrics,
            parsed["verdicts"],
            rationale=rationale if isinstance(rationale, str) else None,
        )

    def _instructions(self) -> str:
        lines = [
            "Você avalia se uma resposta pode ser enviada a um cliente no WhatsApp.",
            "Responda APENAS com JSON no formato "
            '{"verdicts": {"<id-do-criterio>": true|false}, "rationale": "<texto curto>"}.',
            "true significa que o critério foi cumprido.",
            "Julgue todos os critérios abaixo e nenhum outro:",
        ]
        for rubric in sorted(self._rubrics.values(), key=lambda item: item.name):
            for criterion in rubric.criteria:
                lines.append(f"- {criterion.id}: {criterion.description}")
        return "\n".join(lines)

    def _case(self, draft: str, context: JudgeContext) -> str:
        parts = [
            "Conversa até aqui:",
            "\n".join(context.conversation) or "(sem histórico)",
            "",
            "Resposta proposta pelo agente:",
            draft,
            "",
            f"Idioma esperado: {context.language}.",
        ]
        if context.never_say_ai:
            parts.append("A loja exige que o agente nunca admita ser uma IA.")
        if context.knowledge:
            parts += ["", "Base de conhecimento disponível ao agente:"]
            parts += [f"- {chunk}" for chunk in context.knowledge]
        return "\n".join(parts)


#: `(attempt, failed_criteria) -> draft`
Generator = Callable[[int, tuple[str, ...]], Awaitable[str]]
#: `(draft, context) -> judgement`
Judge = Callable[..., Awaitable[Judgement]]


async def guarded_reply(
    generate: Generator,
    judge: Judge,
    *,
    context: JudgeContext | None = None,
    limit: int = REGENERATION_LIMIT,
) -> GuardedOutcome:
    """Generate, judge, and decide what — if anything — may be sent."""
    judgements: list[Judgement] = []
    best: tuple[str, Judgement] | None = None
    feedback: tuple[str, ...] = ()

    for attempt in range(limit + 1):
        draft = await generate(attempt, feedback)

        try:
            judgement = await judge(draft, context)
        except JudgeError as error:
            # Fail-closed, and visibly so: no rubric behind it, so it can never
            # be mistaken for evidence that the draft was fine.
            judgement = Judgement(
                outcome=FAIL, score=0.0, rubrics=(), rationale=f"judge unusable: {error}"
            )
        judgements.append(judgement)

        if judgement.outcome == CRITICAL:
            return GuardedOutcome(
                draft=None,
                judgement=judgement,
                attempts=attempt + 1,
                blocked_by=CRITICAL,
                judgements=tuple(judgements),
            )

        if judgement.outcome == PASS:
            return GuardedOutcome(
                draft=draft,
                judgement=judgement,
                attempts=attempt + 1,
                judgements=tuple(judgements),
            )

        if judgement.usable and (best is None or judgement.score > best[1].score):
            best = (draft, judgement)
        feedback = judgement.failed_criteria

    if best is None:
        # Every attempt failed to produce a usable judgement: there is no
        # evidence this draft is safe, so nothing goes out.
        return GuardedOutcome(
            draft=None,
            judgement=judgements[-1],
            attempts=len(judgements),
            blocked_by="judge_unusable",
            judgements=tuple(judgements),
        )

    draft, judgement = best
    return GuardedOutcome(
        draft=draft,
        judgement=judgement,
        attempts=len(judgements),
        judgements=tuple(judgements),
    )
