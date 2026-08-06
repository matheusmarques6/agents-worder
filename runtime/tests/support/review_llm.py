"""O LLM roteirizado do pós-envio — três papéis numa porta só.

Um turno de auditoria pode gastar três chamadas ao modelo, e as três precisam
ser distinguíveis para que um cenário possa dizer "o juiz reprovou a resposta E
o Judge 1 reprovou a correção":

  1. o **juiz de auditoria** (`judge_async`), no modelo da plataforma;
  2. o **redator da correção**, no modelo do tenant;
  3. o **Judge 1** sobre essa correção, no modelo da plataforma de novo.

O modelo separa (2) dos outros dois — é assim que a realidade se apresenta. A
ORDEM separa (1) de (3): o juiz de auditoria roda uma vez e antes de tudo. Ler o
texto das instruções para distinguir seria amarrar o dublê à redação do prompt.

Os vereditos são construídos a partir dos critérios do PRÓPRIO pedido, como o
`ScriptedLlm` do S9a: rubrica nova não desatualiza o dublê, e o teste nomeia só
o critério que quer reprovar.

Mora em `tests/` porque dublê nenhum viaja na imagem (decisão 8).
"""

import json
from collections.abc import Sequence

from agents_runtime.agent_core.llm import ChatResult, EmbeddingResult, Usage
from agents_runtime.judges.pre_send import JUDGE_MODEL
from tests.support.embedding import embed_text

CORRECTION = "Corrigindo o que enviei: o prazo é de 8 dias úteis, não de 3. 🧡"


class ReviewLlm:
    def __init__(
        self,
        *,
        post_hoc_fails: Sequence[str] = (),
        correction: str = CORRECTION,
        correction_fails: Sequence[str] = (),
        rationale: str = "dublê roteirizado",
    ) -> None:
        self._post_hoc_fails = frozenset(post_hoc_fails)
        self._correction = correction
        self._correction_fails = frozenset(correction_fails)
        self._rationale = rationale
        self.asked: list = []
        self.judged = 0

    async def chat(self, request) -> ChatResult:
        self.asked.append(request)

        if request.model != JUDGE_MODEL:
            return _answer(self._correction, request.model)

        self.judged += 1
        fails = self._post_hoc_fails if self.judged == 1 else self._correction_fails
        verdicts = {criterion: criterion not in fails for criterion in _criteria_of(request)}
        return _answer(
            json.dumps({"verdicts": verdicts, "rationale": self._rationale}), request.model
        )

    async def embed(self, texts: Sequence[str], *, model: str) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple(embed_text(text) for text in texts),
            usage=Usage(input_tokens=len(texts), output_tokens=None, cost_usd=0.0),
            model=model,
        )


def _criteria_of(request) -> list[str]:
    """Os ids que o próprio pedido listou — o dublê lê o contrato que recebeu."""
    instructions = request.messages[0].content
    return [
        line.split(":")[0].removeprefix("- ").strip()
        for line in instructions.splitlines()
        if line.startswith("- ")
    ]


def _answer(text: str, model: str) -> ChatResult:
    return ChatResult(
        text=text,
        usage=Usage(input_tokens=120, output_tokens=40, cost_usd=0.00018),
        model=f"stand-in/{model}",
    )
