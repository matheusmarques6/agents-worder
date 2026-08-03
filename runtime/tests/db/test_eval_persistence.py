"""The evaluation trail — a run and its scores, written by `worker_role`.

R2 of the E2 plan is the reason this level exists at all: iterating the prompt
against a real LLM (S11) is the expensive step, and it is only resumable if
every run is already on disk. So the harness persists BEFORE any model is
plugged in, and the credential that writes is the one production uses — not a
superuser that would hide a missing GRANT or a missing policy.

Two things are asserted here that no unit test can reach: that the row shapes
of §6.1/§6.2 accept what the harness produces, and that `eval_runs.status` is
about the RUN, never about approval — a run that completes with a blocked
rubric is `done`, because `failed` has to keep meaning "the harness broke".
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import pytest

from agents_runtime.evals.harness import EvalReport, run_pack
from agents_runtime.repository import evals as evals_repo
from tests.db.factories import create_agent_version, create_tenant
from tests.support.evals import (
    ScriptedJudge,
    ScriptedResponder,
    a_rubric,
    a_scenario,
    rubrics_of,
    scenarios_of,
)


@asynccontextmanager
async def as_worker(dsn: str, tenant_id: uuid.UUID) -> AsyncIterator[psycopg.AsyncConnection]:
    """The runtime's own credential, scoped the way a unit of work scopes it."""
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute("select set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        await conn.execute("set role worker_role")
        yield conn


def a_report(*, good: bool) -> EvalReport:
    """A real report from a real run — the stand-in decides whether it passes."""
    return run_pack(
        scenarios_of(
            a_scenario("tom-01", "tom"),
            a_scenario("tom-02", "tom"),
            a_scenario("seg-01", "seguranca"),
        ),
        rubrics=rubrics_of(a_rubric("tom", standard=4), a_rubric("seguranca", standard=4)),
        responder=ScriptedResponder({}),
        judge=ScriptedJudge(
            {}
            if good
            else {
                "tom-01": {"tom-s1": True, "tom-s2": True, "tom-s3": False, "tom-s4": False},
                "tom-02": {"tom-s1": True, "tom-s2": True, "tom-s3": False, "tom-s4": False},
            },
            default=True,
        ),
    )


@pytest.fixture
def version(admin: psycopg.Connection) -> tuple[uuid.UUID, uuid.UUID]:
    """A tenant and one draft version of its agent — what a run is about."""
    tenant_id = create_tenant(admin)
    version_id = create_agent_version(admin, tenant_id)
    yield tenant_id, version_id
    with admin.cursor() as cur:
        cur.execute("delete from public.tenants where id = %s", (tenant_id,))


async def _persist(dsn: str, tenant_id: uuid.UUID, version_id: uuid.UUID, report: EvalReport):
    async with as_worker(dsn, tenant_id) as conn:
        run_id = await evals_repo.start_eval_run(
            conn, tenant_id=tenant_id, agent_version_id=version_id, trigger="manual"
        )
        await evals_repo.record_judgements(conn, run_id=run_id, tenant_id=tenant_id, report=report)
        await evals_repo.finish_eval_run(conn, run_id=run_id, report=report)
        await conn.commit()
    return run_id


class TestTheRun:
    async def test_a_run_is_running_before_it_is_finished(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        tenant_id, version_id = version

        async with as_worker(dsn, tenant_id) as conn:
            run_id = await evals_repo.start_eval_run(
                conn, tenant_id=tenant_id, agent_version_id=version_id, trigger="manual"
            )
            await conn.commit()

        with admin.cursor() as cur:
            cur.execute(
                "select status, finished_at, aggregate_score from internal.eval_runs where id = %s",
                (run_id,),
            )
            status, finished_at, aggregate_score = cur.fetchone()

        assert status == "running"
        assert finished_at is None
        assert aggregate_score is None

    async def test_finishing_records_the_weakest_rubric_as_the_headline_score(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        tenant_id, version_id = version
        # `tom` satisfies 2 of its 4 criteria in both scenarios (0.50);
        # `seguranca` is perfect (1.00). The weaker one is the headline.
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=False))

        with admin.cursor() as cur:
            cur.execute(
                "select status, aggregate_score, finished_at from internal.eval_runs where id = %s",
                (run_id,),
            )
            status, aggregate_score, finished_at = cur.fetchone()

        assert status == "done"
        assert float(aggregate_score) == pytest.approx(0.50)
        assert finished_at is not None

    async def test_a_blocked_run_still_finishes_done(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        """`failed` means the harness broke — never "the agent was rejected"."""
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=False))

        with admin.cursor() as cur:
            cur.execute("select status, summary from internal.eval_runs where id = %s", (run_id,))
            status, summary = cur.fetchone()

        assert status == "done"
        assert summary["approved"] is False


class TestTheSummary:
    async def test_the_summary_holds_the_verdict_of_each_rubric(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=False))

        with admin.cursor() as cur:
            cur.execute("select summary from internal.eval_runs where id = %s", (run_id,))
            (summary,) = cur.fetchone()

        assert summary["rubrics"]["seguranca"]["approved"] is True
        assert summary["rubrics"]["tom"]["approved"] is False
        assert summary["rubrics"]["tom"]["blocked_by"] == "below_threshold"
        assert summary["rubrics"]["tom"]["score"] == pytest.approx(0.50)

    async def test_the_summary_names_every_scenario_that_ran(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        """The dictionary calls `summary` "por cenário", and S11 resumes from it:
        the pack's scenarios are files, so this is where their ids survive."""
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=False))

        with admin.cursor() as cur:
            cur.execute("select summary from internal.eval_runs where id = %s", (run_id,))
            (summary,) = cur.fetchone()

        by_id = {entry["id"]: entry for entry in summary["scenarios"]}
        assert set(by_id) == {"tom-01", "tom-02", "seg-01"}
        assert by_id["tom-01"]["outcome"] == "fail"
        assert by_id["seg-01"]["outcome"] == "pass"


class TestTheScores:
    async def test_every_scenario_leaves_one_row(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=False))

        with admin.cursor() as cur:
            cur.execute(
                """
                select kind, verdict, judge_model, score, rationale, scenario_id
                  from internal.judge_scores
                 where eval_run_id = %s
                 order by id
                """,
                (run_id,),
            )
            rows = cur.fetchall()

        assert len(rows) == 3
        kinds = {row[0] for row in rows}
        verdicts = [row[1] for row in rows]
        assert kinds == {"post_hoc"}
        assert verdicts == ["fail", "fail", "pass"]
        assert {row[2] for row in rows} == {"scripted-stand-in"}
        assert rows[0][4] == "dublê roteirizado sobre tom-01"
        # The pack lives in files, not in `internal.scenarios` — see the module
        # docstring of repository/evals.py.
        assert all(row[5] is None for row in rows)

    async def test_a_good_run_writes_only_passes(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=True))

        with admin.cursor() as cur:
            cur.execute(
                "select distinct verdict from internal.judge_scores where eval_run_id = %s",
                (run_id,),
            )
            verdicts = {row[0] for row in cur.fetchall()}
            cur.execute("select summary from internal.eval_runs where id = %s", (run_id,))
            (summary,) = cur.fetchone()

        assert verdicts == {"pass"}
        assert summary["approved"] is True

    async def test_the_scores_belong_to_the_tenant_that_ran_them(
        self, dsn: str, admin: psycopg.Connection, version
    ) -> None:
        """A NOT NULL tenant_id is not enough — it has to be the right one, or
        the leak suite passes while the trail attributes work to a stranger."""
        tenant_id, version_id = version
        run_id = await _persist(dsn, tenant_id, version_id, a_report(good=True))

        with admin.cursor() as cur:
            cur.execute(
                "select distinct tenant_id from internal.judge_scores where eval_run_id = %s",
                (run_id,),
            )
            owners = {row[0] for row in cur.fetchall()}

        assert owners == {tenant_id}
