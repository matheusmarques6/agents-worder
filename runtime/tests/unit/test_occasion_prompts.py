"""A biblioteca de prompts de ocasião — e a etiqueta que a mantém no lugar.

The selection mechanism has existed since the E2 S4: `compose` picks
`scenario_prompts[origin_occasion]`. What never existed was the content, and it
is born here as a FIXTURE — deliberately, because D7 of the E2 still stands:
no prompt content without a traceable source in the onboarding form, and the
thing that maps form answers to layers is the generator of RF-005, which is
E4's.

So the load-bearing test in this file is not about text quality, it is about
provenance. Every file must declare `source: "fixture"`, and a file claiming to
have come from the form is refused — because the generator publishes into
`agent_versions.scenario_prompts`, the database, while this directory stays what
it is. The day the two are mistaken for each other is the day a prompt nobody
can trace reaches a customer with a merchant's name on it.

The other rule with teeth: the library is complete or it does not load. A
missing occasion is not a smaller library — it is a funnel whose conversations
arrive with no scenario layer at all, and the agent answers a PIX reminder like
a stranger saying hello.
"""

import json
import re
from pathlib import Path

import pytest

from agents_runtime.agent_core.occasions import (
    FUNNEL_OCCASIONS,
    default_directory,
    load_occasion_prompts,
    parse_occasion_prompt,
    scenario_prompts,
)
from agents_runtime.agent_core.prompt import (
    OCCASIONS,
    AgentConfig,
    ConversationView,
    TenantPolicy,
    compose,
)
from agents_runtime.evals.pack import known_rfs_from_requirements

REPO_ROOT = Path(__file__).parents[3]
REQUIREMENTS = REPO_ROOT / "core" / "requisitos-e-entidades.md"


def an_entry(**overrides) -> dict:
    return {
        "occasion": "pix_pending",
        "source": "fixture",
        "rfs": ["RF-010"],
        "prompt": "Esta conversa começou porque o cliente gerou um PIX.",
        **overrides,
    }


def a_library(tmp_path: Path, entries) -> Path:
    for entry in entries:
        (tmp_path / f"{entry['occasion']}.json").write_text(
            json.dumps(entry), encoding="utf-8"
        )
    return tmp_path


class TestProvenance:
    def test_a_prompt_claiming_to_come_from_the_form_is_refused(self) -> None:
        """The rule the whole library exists under. Nothing here has a source in
        `core/formulario-perguntas.md`; a file that says otherwise is claiming a
        traceability that does not exist, and the E4 generator — which will have
        it — writes to the database, not to this directory."""
        with pytest.raises(ValueError, match="source is 'form'"):
            parse_occasion_prompt(an_entry(source="form"))

    def test_every_file_shipped_declares_itself_a_fixture(self) -> None:
        library = load_occasion_prompts()

        assert {entry.source for entry in library.values()} == {"fixture"}

    def test_a_ghost_requirement_never_reaches_the_library(self) -> None:
        """Traceability is a project rule, not a courtesy — the same lock the
        eval pack lives under."""
        known = known_rfs_from_requirements(REQUIREMENTS.read_text(encoding="utf-8"))

        cited = {rf for entry in load_occasion_prompts().values() for rf in entry.rfs}

        assert cited <= known
        assert cited


class TestTheLibraryIsComplete:
    def test_it_covers_every_funnel_occasion(self) -> None:
        assert set(load_occasion_prompts()) == set(FUNNEL_OCCASIONS)

    def test_a_missing_occasion_refuses_to_load(self, tmp_path: Path) -> None:
        """Not a smaller library: a funnel whose conversations have no scenario
        layer, discovered in a conversation instead of at load."""
        directory = a_library(
            tmp_path,
            [an_entry(occasion="pix_pending"), an_entry(occasion="cart_abandoned")],
        )

        with pytest.raises(ValueError, match="checkout_abandoned"):
            load_occasion_prompts(directory)

    def test_only_funnel_occasions_live_here(self) -> None:
        """`direct` is the merchant's base prompt with no scenario layer, and
        `campaign` belongs to a milestone that has not happened. Either would be
        content for a mechanism nobody built."""
        with pytest.raises(ValueError, match="not a funnel occasion"):
            parse_occasion_prompt(an_entry(occasion="direct"))

    def test_the_vocabulary_is_a_subset_of_the_schemas(self) -> None:
        """`conversations.origin_occasion` is the authority. A library occasion
        the schema does not know is a layer no conversation can ever select."""
        assert set(FUNNEL_OCCASIONS) <= set(OCCASIONS)


class TestStrictParsing:
    def test_an_empty_prompt_is_refused(self) -> None:
        """`compose` drops an empty layer, so an empty file would make the
        occasion silently stop existing rather than fail."""
        with pytest.raises(ValueError, match="empty prompt"):
            parse_occasion_prompt(an_entry(prompt="   "))

    def test_a_prompt_citing_no_requirement_is_refused(self) -> None:
        with pytest.raises(ValueError, match="traces to nothing"):
            parse_occasion_prompt(an_entry(rfs=[]))

    @pytest.mark.parametrize("rf", ["RF-10", "rf-010", "RNF-044", 10])
    def test_a_citation_outside_the_pattern_is_refused(self, rf) -> None:
        with pytest.raises(ValueError, match="RF-xxx"):
            parse_occasion_prompt(an_entry(rfs=[rf]))

    def test_an_unexpected_field_is_refused(self) -> None:
        """Never "use what parsed": a field nobody reads is a field somebody
        thought they had configured."""
        with pytest.raises(ValueError, match="extra"):
            parse_occasion_prompt(an_entry(tone="informal"))


class TestWhatTheContentMayNotDo:
    @pytest.mark.parametrize("occasion", FUNNEL_OCCASIONS)
    def test_no_prompt_carries_a_commercial_promise(self, occasion: str) -> None:
        """This library ships to every tenant that has no generated prompt yet.
        A discount written into it is a promise every merchant makes without
        having agreed to it — and one that a human then has to honour."""
        body = load_occasion_prompts()[occasion].prompt

        assert not re.search(r"\d\s*%", body)
        assert "R$" not in body

    @pytest.mark.parametrize("occasion", FUNNEL_OCCASIONS)
    def test_every_prompt_sends_the_hard_cases_to_a_person(self, occasion: str) -> None:
        """The occasion the conversation started in is exactly where an agent
        runs out of road — refund, complaint, an exception to policy. A scenario
        layer that never names the exit is a layer that keeps the customer."""
        assert "humano" in load_occasion_prompts()[occasion].prompt


class TestTheConsumer:
    def test_the_library_is_the_shape_the_column_holds(self) -> None:
        """`agent_versions.scenario_prompts` and `AgentConfig.scenario_prompts`
        read a `{occasion: text}` mapping. The E4 generator will produce the same
        shape from the form — which is what makes this a library and not three
        files nobody can use."""
        mapping = scenario_prompts(load_occasion_prompts())

        assert set(mapping) == set(FUNNEL_OCCASIONS)
        assert all(isinstance(text, str) and text for text in mapping.values())

    @pytest.mark.parametrize("occasion", FUNNEL_OCCASIONS)
    def test_the_selection_mechanism_of_the_e2_finally_selects_something(
        self, occasion: str
    ) -> None:
        """The mechanism has existed since the E2 S4 and had nothing to pick.
        This is the end-to-end of the step: a conversation born of a funnel gets
        the layer of ITS occasion, in the position RF-010 fixes."""
        library = load_occasion_prompts()
        config = AgentConfig(
            model="claude-sonnet-5",
            base_prompt="Você é o atendente da loja.",
            scenario_prompts=scenario_prompts(library),
        )

        layers = compose(
            config,
            TenantPolicy(primary_language="pt-BR", never_say_ai=False),
            ConversationView(occasion=occasion, contact_language=None),
        )

        scenario = next(layer for layer in layers if layer.name == "scenario")
        assert scenario.body == library[occasion].prompt
        assert [layer.name for layer in layers] == ["base", "scenario"]

    def test_a_direct_conversation_still_gets_no_scenario_layer(self) -> None:
        """The negative that keeps the library honest: `direct` is not a funnel,
        and handing it somebody else's occasion would tell the agent a customer
        abandoned something they never touched."""
        config = AgentConfig(
            model="claude-sonnet-5",
            base_prompt="Você é o atendente da loja.",
            scenario_prompts=scenario_prompts(load_occasion_prompts()),
        )

        layers = compose(
            config,
            TenantPolicy(primary_language="pt-BR", never_say_ai=False),
            ConversationView(occasion="direct", contact_language=None),
        )

        assert [layer.name for layer in layers] == ["base"]


def test_the_default_directory_is_derived_from_the_package() -> None:
    """Never from the working directory: the process starts inside a container
    whose cwd is not the repository."""
    assert default_directory().is_dir()
    assert default_directory().name == "occasions"
