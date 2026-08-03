"""The synthetic scenario pack — loading, strict parsing, traceability.

ADR-12 rules this module's content policy: scenarios are 100% synthetic,
never copied from real conversations — secondary use is SUSPENDED, and the
pack is exactly the kind of copy the suspension forbids.

Traceability is a project rule, not a courtesy: every scenario AND every
rubric cites the RF(s) it validates, and `validate_pack` checks both sets of
citations against the vocabulary extracted from
`core/requisitos-e-entidades.md` — a ghost requirement fails the gate before
any LLM exists.

Scenario messages stay in PT-BR: they are the conversations the agent will
face, so they are content, not code.
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
    return ValueError(f"invalid scenario: {reason}")


def parse_scenario(raw: dict) -> Scenario:
    if set(raw) != _SCENARIO_FIELDS:
        raise _reject(
            f"wrong fields: missing {sorted(_SCENARIO_FIELDS - set(raw))}, "
            f"extra {sorted(set(raw) - _SCENARIO_FIELDS)}"
        )
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise _reject("id must be non-empty text")
    if not isinstance(raw["rubric"], str) or not raw["rubric"]:
        raise _reject("rubric must be non-empty text")

    rfs = raw["rfs"]
    if not isinstance(rfs, list) or not rfs:
        raise _reject(f"{raw['id']}: a scenario with no RF validates nothing")
    for rf in rfs:
        if not isinstance(rf, str) or not RF_PATTERN.match(rf):
            raise _reject(f"{raw['id']}: rf outside the RF-xxx pattern: {rf!r}")

    if raw["occasion"] not in OCCASIONS:
        raise _reject(f"{raw['id']}: unknown occasion {raw['occasion']!r}")

    raw_messages = raw["messages"]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise _reject(f"{raw['id']}: scenario without messages")
    messages = []
    for entry in raw_messages:
        if not isinstance(entry, dict) or set(entry) != _MESSAGE_FIELDS:
            raise _reject(f"{raw['id']}: message with the wrong shape: {entry!r}")
        if entry["author"] not in AUTHORS:
            raise _reject(f"{raw['id']}: unknown author {entry['author']!r}")
        if not isinstance(entry["text"], str) or not entry["text"]:
            raise _reject(f"{raw['id']}: message without text")
        messages.append(Message(author=entry["author"], text=entry["text"]))

    if not isinstance(raw["expectation"], str) or not raw["expectation"]:
        raise _reject(f"{raw['id']}: empty expectation — the judge would have no contract")

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
            raise ValueError(f"duplicate rubric: {rubric.name}")
        rubrics[rubric.name] = rubric
    return rubrics


def load_pack(directory: Path) -> tuple[Scenario, ...]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            scenarios.append(parse_scenario(raw))
    return tuple(scenarios)


def validate_pack(
    scenarios,
    *,
    rubrics: dict[str, Rubric],
    known_rfs: frozenset[str] | set[str],
) -> None:
    """The traceability lock. Raises on the first lie; silent when honest."""
    # Both ends cite RFs, so both ends are checked: leaving the rubric out
    # would hand a free pass to the side that FAILS the agent.
    for name, rubric in sorted(rubrics.items()):
        for rf in rubric.rfs:
            if rf not in known_rfs:
                raise ValueError(
                    f"rubric {name}: cites {rf}, which does not exist in "
                    "core/requisitos-e-entidades.md — traceability broken"
                )

    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise ValueError(f"duplicate scenario id: {scenario.id}")
        seen.add(scenario.id)

        if scenario.rubric not in rubrics:
            raise ValueError(f"{scenario.id}: unknown rubric {scenario.rubric!r}")

        for rf in scenario.rfs:
            if rf not in known_rfs:
                raise ValueError(
                    f"{scenario.id}: cites {rf}, which does not exist in "
                    "core/requisitos-e-entidades.md — traceability broken"
                )
