"""The evaluation trail's SQL — `eval_runs` and `judge_scores` (§6.1, §6.2).

Persistence is what makes R2 of the E2 plan survivable: iterating a prompt
against a real LLM is the expensive step, so every run lands on disk and S11
resumes from it instead of paying for the same scenarios twice.

Like the rest of this layer, functions take a connection and never open or
commit one — the caller owns the transaction and the `SET LOCAL app.tenant_id`
scope that goes with it.

Two shape decisions, both forced by the schema and neither hidden in a commit
message:

  * **`kind = 'post_hoc'`.** The CHECK offers `pre_send | post_hoc`, and an
    eval run is neither a live gate nor a review of a sent message. Of the two
    it is `post_hoc`: what `pre_send` means is "this score decided whether a
    reply left the outbox", and no eval score ever does.

  * **`scenario_id` stays NULL.** The E2 pack lives in versioned JSON files
    (`runtime/evals/pack/`), not in `internal.scenarios`, and that table has no
    natural key a file could match on. The scenario ids survive in
    `eval_runs.summary`, which the data dictionary already defines as "por
    cenário". Linking the two needs an `external_id` column — additive, and it
    belongs to the milestone that gives merchants their own scenarios (E4),
    not to this one.
"""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from agents_runtime.evals.harness import EvalReport

# The trail's own vocabulary, kept next to the SQL that writes it.
_EVAL_JUDGE_KIND = "post_hoc"


async def start_eval_run(
    conn: psycopg.AsyncConnection,
    *,
    tenant_id: UUID,
    agent_version_id: UUID,
    trigger: str,
) -> UUID:
    """Open a run. It stays `running` until `finish_eval_run` closes it."""
    cursor = await conn.execute(
        """
        insert into internal.eval_runs (tenant_id, agent_version_id, trigger, status)
        values (%s, %s, %s, 'running')
        returning id
        """,
        (tenant_id, agent_version_id, trigger),
    )
    return (await cursor.fetchone())[0]


async def record_judgements(
    conn: psycopg.AsyncConnection,
    *,
    run_id: UUID,
    tenant_id: UUID,
    report: EvalReport,
) -> None:
    """One row per scenario judged, in the order the pack ran."""
    async with conn.cursor() as cursor:
        for result in report.results:
            await cursor.execute(
                """
                insert into internal.judge_scores
                    (tenant_id, kind, eval_run_id, judge_model, score, verdict, rationale)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    _EVAL_JUDGE_KIND,
                    run_id,
                    report.judge_model,
                    result.passed_ratio,
                    result.outcome,
                    result.rationale,
                ),
            )


async def finish_eval_run(
    conn: psycopg.AsyncConnection,
    *,
    run_id: UUID,
    report: EvalReport,
) -> None:
    """Close the run.

    `status` describes the RUN, not the agent: a completed run whose rubrics
    were all blocked is `done`, because `failed` has to keep meaning "the
    harness itself broke" — collapsing the two would make a crashed run and a
    rejected version indistinguishable on the screen that lists them.

    `aggregate_score` is the WEAKEST rubric, never a mean over the pack: the
    gate is per rubric (D3), so the number that summarises a run is the one
    that decides it.
    """
    await conn.execute(
        """
        update internal.eval_runs
           set status = %s,
               aggregate_score = %s,
               summary = %s,
               finished_at = now()
         where id = %s
        """,
        (
            "done",
            report.weakest_score,
            Jsonb(summary_of(report)),
            run_id,
        ),
    )


def summary_of(report: EvalReport) -> dict:
    """The run's report as jsonb — per rubric and, as the dictionary says, per scenario."""
    return {
        "approved": report.approved,
        "judge_model": report.judge_model,
        "rubrics": {
            name: {
                "score": aggregate.score,
                "threshold": aggregate.threshold,
                "scenarios": aggregate.scenarios,
                "criticals": aggregate.criticals,
                "approved": aggregate.approved,
                "blocked_by": aggregate.blocked_by,
            }
            for name, aggregate in report.aggregates.items()
        },
        "scenarios": [
            {
                "id": result.scenario_id,
                "rubric": result.rubric,
                "outcome": result.outcome,
                "score": result.passed_ratio,
            }
            for result in report.results
        ],
    }
