"""The eval harness — running the pack and aggregating PER RUBRIC (D3).

`core/testes-e-cicd.md` §5 states the approval criterion in one line: "pontuação
agregada ≥ limite mínimo definido por rubrica; zero veredito `critical`". D3 of
the E2 plan sharpens it — the floor is per rubric, NEVER an aggregate over the
whole pack, because a strong rubric would otherwise pay for a broken one and
the activation gate would approve an agent that fails at exactly one thing.

This suite is pure: the responder and the judge are scripted stand-ins, so the
harness is proved before any LLM exists. Persistence lives in the `db` suite.

Every expected number here is SPELLED OUT rather than recomputed from the
production formula — a test that recites the implementation moves with it and
catches nothing (the lesson of the S4 layer-order sabotage, decisão 81).
"""

import pytest

from agents_runtime.evals.harness import run_pack
from tests.support.evals import (
    ScriptedJudge,
    ScriptedResponder,
    a_rubric,
    a_scenario,
    rubrics_of,
    scenarios_of,
)


class TestTheStandInDecidesTheOutcome:
    """"dublê ruim reprova, dublê bom aprova" — the S3 acceptance line."""

    def test_a_good_stand_in_approves_the_rubric(self) -> None:
        rubrics = rubrics_of(a_rubric("tom", standard=4))
        scenarios = scenarios_of(a_scenario("tom-01", "tom"), a_scenario("tom-02", "tom"))

        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge({}, default=True),
        )

        assert report.approved is True
        assert report.aggregates["tom"].approved is True
        assert report.aggregates["tom"].blocked_by is None

    def test_a_bad_stand_in_fails_the_rubric(self) -> None:
        rubrics = rubrics_of(a_rubric("tom", standard=4))
        scenarios = scenarios_of(a_scenario("tom-01", "tom"), a_scenario("tom-02", "tom"))

        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge({}, default=False),
        )

        assert report.approved is False
        assert report.aggregates["tom"].approved is False
        assert report.aggregates["tom"].blocked_by == "below_threshold"


class TestAggregationIsPerRubric:
    def test_the_score_is_the_mean_of_the_criteria_ratios(self) -> None:
        # Four criteria. One scenario satisfies all four, the other exactly two:
        # 1.00 and 0.50, so the rubric scores 0.75 — below the 0.85 floor.
        rubrics = rubrics_of(a_rubric("tom", standard=4))
        scenarios = scenarios_of(a_scenario("tom-01", "tom"), a_scenario("tom-02", "tom"))
        judge = ScriptedJudge(
            {
                "tom-01": {"tom-s1": True, "tom-s2": True, "tom-s3": True, "tom-s4": True},
                "tom-02": {"tom-s1": True, "tom-s2": True, "tom-s3": False, "tom-s4": False},
            }
        )

        report = run_pack(
            scenarios, rubrics=rubrics, responder=ScriptedResponder({}), judge=judge
        )

        assert report.aggregates["tom"].score == pytest.approx(0.75)
        assert report.aggregates["tom"].scenarios == 2
        assert report.aggregates["tom"].approved is False

    def test_a_failing_rubric_never_drags_a_passing_one_down(self) -> None:
        """D3 spelled out: the floor is per rubric, so they are judged apart."""
        rubrics = rubrics_of(a_rubric("tom", standard=4), a_rubric("escopo", standard=4))
        scenarios = scenarios_of(
            a_scenario("tom-01", "tom"), a_scenario("escopo-01", "escopo")
        )
        judge = ScriptedJudge(
            {
                "tom-01": {"tom-s1": False, "tom-s2": False, "tom-s3": False, "tom-s4": False},
                "escopo-01": {
                    "escopo-s1": True,
                    "escopo-s2": True,
                    "escopo-s3": True,
                    "escopo-s4": True,
                },
            }
        )

        report = run_pack(
            scenarios, rubrics=rubrics, responder=ScriptedResponder({}), judge=judge
        )

        assert report.aggregates["escopo"].approved is True
        assert report.aggregates["escopo"].score == pytest.approx(1.0)
        assert report.aggregates["tom"].approved is False
        assert report.aggregates["tom"].score == pytest.approx(0.0)
        # One broken rubric is enough to hold the version back — but only the
        # broken one is marked broken.
        assert report.approved is False

    def test_the_weakest_rubric_is_the_runs_headline_number(self) -> None:
        rubrics = rubrics_of(a_rubric("tom", standard=4), a_rubric("escopo", standard=4))
        scenarios = scenarios_of(
            a_scenario("tom-01", "tom"), a_scenario("escopo-01", "escopo")
        )
        judge = ScriptedJudge(
            {
                "tom-01": {"tom-s1": True, "tom-s2": True, "tom-s3": True, "tom-s4": False},
                "escopo-01": {
                    "escopo-s1": True,
                    "escopo-s2": True,
                    "escopo-s3": True,
                    "escopo-s4": True,
                },
            }
        )

        report = run_pack(
            scenarios, rubrics=rubrics, responder=ScriptedResponder({}), judge=judge
        )

        # 3 of 4 criteria on the weaker rubric.
        assert report.weakest_score == pytest.approx(0.75)


class TestZeroCriticalIsNonNegotiable:
    def test_one_critical_vetoes_a_rubric_scoring_well_above_the_floor(self) -> None:
        """A veto is not a score: D3 says zero `critical`, whatever the average."""
        rubrics = rubrics_of(a_rubric("seguranca", standard=4, critical=1))
        scenarios = scenarios_of(*(a_scenario(f"seg-0{i}", "seguranca") for i in range(1, 5)))
        perfect = {f"seguranca-s{i}": True for i in range(1, 5)} | {"seguranca-c1": True}
        leaked_the_prompt = dict(perfect) | {"seguranca-c1": False}

        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge(
                {
                    "seg-01": perfect,
                    "seg-02": perfect,
                    "seg-03": perfect,
                    "seg-04": leaked_the_prompt,
                }
            ),
        )

        # Three perfect runs and one that satisfied 4 of 5: mean 0.95, well
        # above the 0.85 floor — and still blocked.
        assert report.aggregates["seguranca"].score == pytest.approx(0.95)
        assert report.aggregates["seguranca"].criticals == 1
        assert report.aggregates["seguranca"].approved is False
        assert report.aggregates["seguranca"].blocked_by == "critical"
        assert report.approved is False

    def test_the_offending_scenario_is_named_in_the_results(self) -> None:
        rubrics = rubrics_of(a_rubric("seguranca", standard=2, critical=1))
        scenarios = scenarios_of(
            a_scenario("seg-01", "seguranca"), a_scenario("seg-02", "seguranca")
        )
        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge(
                {
                    "seg-01": {"seguranca-s1": True, "seguranca-s2": True, "seguranca-c1": True},
                    "seg-02": {"seguranca-s1": True, "seguranca-s2": True, "seguranca-c1": False},
                }
            ),
        )

        by_id = {result.scenario_id: result for result in report.results}
        assert by_id["seg-01"].outcome == "pass"
        assert by_id["seg-02"].outcome == "critical"
        assert by_id["seg-02"].rubric == "seguranca"


class TestTheWiring:
    def test_the_judge_reads_the_reply_the_responder_produced(self) -> None:
        """Without this the harness could score a reply nobody generated."""
        rubrics = rubrics_of(a_rubric("factual", standard=2))
        scenarios = scenarios_of(a_scenario("fac-01", "factual"))
        judge = ScriptedJudge({})

        run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({"fac-01": "Entregamos em Manaus em 7 dias úteis."}),
            judge=judge,
        )

        assert judge.replies_seen["fac-01"] == "Entregamos em Manaus em 7 dias úteis."

    def test_the_judges_rationale_survives_into_the_result(self) -> None:
        rubrics = rubrics_of(a_rubric("factual", standard=2))
        scenarios = scenarios_of(a_scenario("fac-01", "factual"))

        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge({}),
        )

        assert report.results[0].rationale == "dublê roteirizado sobre fac-01"

    def test_the_report_carries_the_judge_that_produced_it(self) -> None:
        """`judge_scores.judge_model` is NOT NULL — the trail names its judge."""
        rubrics = rubrics_of(a_rubric("factual", standard=2))
        scenarios = scenarios_of(a_scenario("fac-01", "factual"))

        report = run_pack(
            scenarios,
            rubrics=rubrics,
            responder=ScriptedResponder({}),
            judge=ScriptedJudge({}),
        )

        assert report.judge_model == "scripted-stand-in"


class TestTheHarnessRefusesToGuess:
    def test_a_rubric_with_no_scenario_is_rejected(self) -> None:
        """An unexercised rubric reporting "approved" is the false green D3 exists to stop."""
        rubrics = rubrics_of(a_rubric("tom", standard=2), a_rubric("seguranca", standard=2))
        scenarios = scenarios_of(a_scenario("tom-01", "tom"))

        with pytest.raises(ValueError, match="seguranca"):
            run_pack(
                scenarios,
                rubrics=rubrics,
                responder=ScriptedResponder({}),
                judge=ScriptedJudge({}),
            )

    def test_a_scenario_citing_an_unknown_rubric_is_rejected(self) -> None:
        rubrics = rubrics_of(a_rubric("tom", standard=2))
        scenarios = scenarios_of(a_scenario("fantasma-01", "rubrica-que-nao-existe"))

        with pytest.raises(ValueError, match="rubrica-que-nao-existe"):
            run_pack(
                scenarios,
                rubrics=rubrics,
                responder=ScriptedResponder({}),
                judge=ScriptedJudge({}),
            )

    def test_an_empty_pack_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            run_pack(
                (),
                rubrics=rubrics_of(a_rubric("tom", standard=2)),
                responder=ScriptedResponder({}),
                judge=ScriptedJudge({}),
            )

    def test_a_partial_judgement_is_the_judges_bug_and_surfaces(self) -> None:
        """`score()` already refuses verdicts that do not cover the criteria.

        The harness must not swallow that into a `fail`: a judge that answered
        half the rubric produced no judgement at all, and silently scoring it
        would turn a broken judge into a bad agent.
        """
        rubrics = rubrics_of(a_rubric("tom", standard=3))
        scenarios = scenarios_of(a_scenario("tom-01", "tom"))

        with pytest.raises(ValueError, match="do not cover"):
            run_pack(
                scenarios,
                rubrics=rubrics,
                responder=ScriptedResponder({}),
                judge=ScriptedJudge({"tom-01": {"tom-s1": True}}),
            )
