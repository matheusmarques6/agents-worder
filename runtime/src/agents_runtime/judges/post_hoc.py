"""Judge 2 — the audit of a reply the customer already has (fluxo 5, RF-015).

Judge 1 decides whether a draft may leave. This one reads what LEFT, after the
fact, and it exists because a gate that is never confronted with what it let
through is decoration.

**Same rubrics as Judge 1**, deliberately: the gate that blocks a send, the gate
that activates a version and the gate that audits a sent message all speak the
criteria of S1 (`runtime/evals/rubrics/`). Three vocabularies for one question
would mean three different answers to "was this reply acceptable?", and the one
that reaches the merchant's screen would be whichever ran last.

What differs is the CONSEQUENCE, and the judge is told so: it cannot veto
anything. A `critical` here means the customer already read a bad message, and
the only repair available is another message — which is why the correction path
exists, and why it goes through Judge 1 like any other reply.

**The model is platform-fixed** (D1, decisão 79), the same constant Judge 1
uses. A safety gate a customer can reconfigure is not a gate, and an audit a
customer can make cheaper is not an audit.
"""

import json
from collections.abc import Mapping

from agents_runtime.agent_core.llm import ChatRequest, LlmPort, Message
from agents_runtime.evals.rubrics import Rubric
from agents_runtime.judges.pre_send import (
    JUDGE_MODEL,
    JudgeContext,
    JudgeError,
    Judgement,
    judge_verdicts,
)

#: The purpose this judge's cost is billed under (`internal.llm_calls.purpose`).
#: Separate from `judge_pre` on the cost screen, because the two answer
#: different questions and only one of them is on the customer's critical path.
JUDGE_PURPOSE = "judge_async"


class PostHocJudge:
    """The judge that calls a model over a message that was already delivered.

    Wrap it in `MeteredLlm(purpose=JUDGE_PURPOSE)` at composition and the
    audit's cost shows up separately from the gate's.
    """

    def __init__(self, llm: LlmPort, rubrics: Mapping[str, Rubric]) -> None:
        self._llm = llm
        self._rubrics = dict(rubrics)

    async def __call__(self, reply: str, context: JudgeContext) -> Judgement:
        request = ChatRequest(
            # Never `context`, never a config: the model of a gate is the
            # platform's, whatever the tenant runs its agent on.
            model=JUDGE_MODEL,
            messages=(
                Message(role="system", content=self._instructions()),
                Message(role="user", content=self._case(reply, context)),
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
            "Você audita uma resposta que JÁ FOI ENVIADA a um cliente no WhatsApp.",
            "A mensagem não pode mais ser impedida: avalie o que ela de fato disse, "
            "não o que você teria escrito.",
            "Responda APENAS com JSON no formato "
            '{"verdicts": {"<id-do-criterio>": true|false}, "rationale": "<texto curto>"}.',
            "true significa que o critério foi cumprido.",
            "Julgue todos os critérios abaixo e nenhum outro:",
        ]
        for rubric in sorted(self._rubrics.values(), key=lambda item: item.name):
            for criterion in rubric.criteria:
                lines.append(f"- {criterion.id}: {criterion.description}")
        return "\n".join(lines)

    def _case(self, reply: str, context: JudgeContext) -> str:
        parts = [
            "Conversa até aqui:",
            "\n".join(context.conversation) or "(sem histórico)",
            "",
            "Resposta que o agente enviou:",
            reply,
            "",
            f"Idioma esperado: {context.language}.",
        ]
        if context.never_say_ai:
            parts.append("A loja exige que o agente nunca admita ser uma IA.")
        if context.knowledge:
            parts += ["", "Base de conhecimento disponível ao agente:"]
            parts += [f"- {chunk}" for chunk in context.knowledge]
        return "\n".join(parts)
