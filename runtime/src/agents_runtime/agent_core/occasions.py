"""A biblioteca de prompts de ocasião de funil — **fixture rotulada**.

The selection mechanism has existed since the E2 S4: `compose` picks
`scenario_prompts[conversation.origin_occasion]` and refuses an occasion outside
the schema's vocabulary. What never existed was the CONTENT, and this is where
it is born — for the three funnel occasions, and as a fixture, on purpose.

**Why a fixture and not a product.** D7 of the E2 stands: no prompt content
without a traceable source in the onboarding form (`core/formulario-
perguntas.md`), and the thing that maps form answers to prompt layers is the
generator of RF-005, which is E4's. Anything in this directory has no such
source — it was written by hand to make the mechanism testable and to give a
merchant something coherent before their own answers exist. So every file has
to SAY so:

    "source": "fixture"

and the loader refuses any other value. A file here claiming `"form"` would be
claiming a provenance that does not exist, and the whole reason the rule has
teeth is that the E4 generator will publish into `agent_versions.scenario_
prompts` — the database — while this directory stays what it is. The two must
never be mistaken for each other, and the label is what keeps them apart.

Only the three FUNNEL occasions live here. `direct` is the merchant's own base
prompt with no scenario layer at all, and `campaign` belongs to a milestone that
has not happened; a file for either would be content for a mechanism nobody has
built.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import agents_runtime
from agents_runtime.evals.rubrics import RF_PATTERN

#: The occasions a FUNNEL produces (`funnels.occasion`, migration 0002 of E3) —
#: a strict subset of `prompt.OCCASIONS`, which also knows `direct` and
#: `campaign`.
FUNNEL_OCCASIONS = ("pix_pending", "checkout_abandoned", "cart_abandoned")

#: The only provenance this directory may claim. See the module docstring.
FIXTURE = "fixture"

_FIELDS = {"occasion", "source", "rfs", "prompt"}


@dataclass(frozen=True, slots=True)
class OccasionPrompt:
    occasion: str
    source: str
    rfs: tuple[str, ...]
    prompt: str


def _reject(reason: str) -> ValueError:
    return ValueError(f"invalid occasion prompt: {reason}")


def parse_occasion_prompt(raw: dict) -> OccasionPrompt:
    if set(raw) != _FIELDS:
        raise _reject(
            f"wrong fields: missing {sorted(_FIELDS - set(raw))}, "
            f"extra {sorted(set(raw) - _FIELDS)}"
        )

    if raw["occasion"] not in FUNNEL_OCCASIONS:
        raise _reject(
            f"{raw['occasion']!r} is not a funnel occasion "
            f"({', '.join(FUNNEL_OCCASIONS)}) — `direct` has no scenario layer "
            "and `campaign` has no milestone"
        )

    if raw["source"] != FIXTURE:
        # The label is the rule. A file here claiming to come from the form
        # would be claiming a provenance that does not exist — the generator of
        # RF-005 writes into `agent_versions.scenario_prompts`, never here.
        raise _reject(
            f"{raw['occasion']}: source is {raw['source']!r}; everything in this "
            f"library is {FIXTURE!r} until the E4 generator exists"
        )

    rfs = raw["rfs"]
    if not isinstance(rfs, list) or not rfs:
        raise _reject(f"{raw['occasion']}: a prompt citing no RF traces to nothing")
    for rf in rfs:
        if not isinstance(rf, str) or not RF_PATTERN.match(rf):
            raise _reject(f"{raw['occasion']}: rf outside the RF-xxx pattern: {rf!r}")

    if not isinstance(raw["prompt"], str) or not raw["prompt"].strip():
        raise _reject(
            f"{raw['occasion']}: empty prompt — `compose` would drop the layer "
            "and the occasion would silently stop existing"
        )

    return OccasionPrompt(
        occasion=raw["occasion"],
        source=raw["source"],
        rfs=tuple(rfs),
        prompt=raw["prompt"].strip(),
    )


def default_directory() -> Path:
    """`runtime/prompts/occasions` in the repository, `/app/prompts/occasions`
    in the image — derived from the installed package, never from the working
    directory, because the process starts inside a container whose cwd is not
    the repository. Same device `default_rubrics_directory` uses."""
    return Path(agents_runtime.__file__).parents[2] / "prompts" / "occasions"


def load_occasion_prompts(directory: Path | None = None) -> dict[str, OccasionPrompt]:
    """The library, keyed by occasion. Complete or not at all.

    A missing occasion is not a smaller library, it is a funnel whose
    conversations arrive with no scenario layer — the agent answering a PIX
    reminder as if it were a stranger saying hello. That is worth failing on at
    load, not discovering in a conversation.
    """
    directory = directory or default_directory()
    library: dict[str, OccasionPrompt] = {}

    for path in sorted(directory.glob("*.json")):
        entry = parse_occasion_prompt(json.loads(path.read_text(encoding="utf-8")))
        if entry.occasion in library:
            raise ValueError(f"duplicate occasion prompt: {entry.occasion}")
        library[entry.occasion] = entry

    missing = sorted(set(FUNNEL_OCCASIONS) - set(library))
    if missing:
        raise ValueError(
            f"the occasion library is incomplete: {', '.join(missing)} — "
            "a funnel whose conversations have no scenario layer"
        )

    return library


def scenario_prompts(library: dict[str, OccasionPrompt]) -> dict[str, str]:
    """The exact shape `agent_versions.scenario_prompts` holds and `AgentConfig`
    reads — which is what makes this a library rather than three files nobody
    can use. The E4 generator will produce the same shape from the form."""
    return {occasion: entry.prompt for occasion, entry in library.items()}
