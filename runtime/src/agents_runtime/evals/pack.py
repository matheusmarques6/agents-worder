"""The synthetic scenario pack — loading, strict parsing, traceability.

ADR-12 rules this module's content policy: scenarios are 100% synthetic,
never copied from real conversations — secondary use is SUSPENDED, and the
pack is exactly the kind of copy the suspension forbids.

Traceability is a project rule, not a courtesy: every scenario cites the
RF(s) it validates, and `validate_pack` checks the citations against the
vocabulary extracted from `core/requisitos-e-entidades.md` — a scenario
citing a ghost requirement fails the gate before any LLM exists.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agents_runtime.evals.rubrics import RF_PATTERN, Rubric, parse_rubric

# The occasions the schema knows (conversations.origin_occasion). The E2 pack
# speaks 'direct'; the funnel occasions join in E3 — the vocabulary is already
# complete so the mechanism never grows ad hoc values.
OCCASIONS = ("pix_pending", "checkout_abandoned", "cart_abandoned", "direct", "campaign")

AUTHORS = ("contact", "agent")

_SCENARIO_FIELDS = {"id", "rubric", "rfs", "occasion", "messages", "expectation"}
_MESSAGE_FIELDS = {"author", "text"}


@dataclass(frozen=True, slots=True)
class Message:
    author: str
    text: str


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    rubric: str
    rfs: tuple[str, ...]
    occasion: str
    messages: tuple[Message, ...]
    expectation: str


def _reject(reason: str) -> ValueError:
    return ValueError(f"cenário inválido: {reason}")


def parse_scenario(raw: dict) -> Scenario:
    if set(raw) != _SCENARIO_FIELDS:
        raise _reject(
            f"campos errados: faltam {sorted(_SCENARIO_FIELDS - set(raw))}, "
            f"sobram {sorted(set(raw) - _SCENARIO_FIELDS)}"
        )
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise _reject("id deve ser texto não-vazio")
    if not isinstance(raw["rubric"], str) or not raw["rubric"]:
        raise _reject("rubric deve ser texto não-vazio")

    rfs = raw["rfs"]
    if not isinstance(rfs, list) or not rfs:
        raise _reject(f"{raw['id']}: cenário sem RF não valida coisa nenhuma")
    for rf in rfs:
        if not isinstance(rf, str) or not RF_PATTERN.match(rf):
            raise _reject(f"{raw['id']}: rf fora do padrão RF-xxx: {rf!r}")

    if raw["occasion"] not in OCCASIONS:
        raise _reject(f"{raw['id']}: ocasião desconhecida {raw['occasion']!r}")

    raw_messages = raw["messages"]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _reject(f"{raw['id']}: cenário sem mensagens")
    messages = []
    for entry in raw_messages:
        if not isinstance(entry, dict) or set(entry) != _MESSAGE_FIELDS:
            raise _reject(f"{raw['id']}: mensagem com forma errada: {entry!r}")
        if entry["author"] not in AUTHORS:
            raise _reject(f"{raw['id']}: autor desconhecido {entry['author']!r}")
        if not isinstance(entry["text"], str) or not entry["text"]:
            raise _reject(f"{raw['id']}: mensagem sem texto")
        messages.append(Message(author=entry["author"], text=entry["text"]))

    if not isinstance(raw["expectation"], str) or not raw["expectation"]:
        raise _reject(f"{raw['id']}: expectation vazia — o judge não teria contrato")

    return Scenario(
        id=raw["id"],
        rubric=raw["rubric"],
        rfs=tuple(rfs),
        occasion=raw["occasion"],
        messages=tuple(messages),
        expectation=raw["expectation"],
    )


def known_rfs_from_requirements(text: str) -> frozenset[str]:
    """The RF vocabulary, extracted from the canonical doc itself."""
    return frozenset(re.findall(r"\bRF-\d{3}\b", text))


def load_rubrics(directory: Path) -> dict[str, Rubric]:
    rubrics: dict[str, Rubric] = {}
    for path in sorted(directory.glob("*.json")):
        rubric = parse_rubric(json.loads(path.read_text(encoding="utf-8")))
        if rubric.name in rubrics:
            raise ValueError(f"rubrica duplicada: {rubric.name}")
        rubrics[rubric.name] = rubric
    return rubrics


def load_pack(directory: Path) -> tuple[Scenario, ...]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            scenarios.append(parse_scenario(raw))
    return tuple(scenarios)


def validate_pack(
    scenarios, *, rubrics: dict, known_rfs: frozenset[str] | set[str]
) -> None:
    """The traceability lock. Raises on the first lie; silent when honest."""
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise ValueError(f"id de cenário duplicado: {scenario.id}")
        seen.add(scenario.id)

        if scenario.rubric not in rubrics:
            raise ValueError(f"{scenario.id}: rubrica desconhecida {scenario.rubric!r}")

        for rf in scenario.rfs:
            if rf not in known_rfs:
                raise ValueError(
                    f"{scenario.id}: cita {rf}, que não existe em "
                    "core/requisitos-e-entidades.md — rastreabilidade quebrada"
                )
