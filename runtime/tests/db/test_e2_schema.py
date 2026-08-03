"""E2 · S2 — the schema slice of the milestone, and the two decisions it freezes.

Three things are asserted here that no later step can re-decide cheaply:

· **One `active` version per tenant is the DATABASE's job** (D6, integrity note 1
  of the data dictionary). The source of truth is the partial unique index — not
  an FK cache on `tenants`, not application code. A second activation must be an
  error, not a race the hub happens to lose.

· **The agent's model is per-tenant configuration** (D1 as amended, decision 79):
  it lives in the version's own row with `claude-sonnet-5` as the default, so
  changing it is config, never a deploy. Judge 1 is NOT here — it is fixed by the
  platform, and a safety gate that a customer could reconfigure is not a gate.

· **The embedding dimension is frozen at 1536** (D2). This is the reason D2 had
  to be answered before S2 at all: expand-contract makes a later change to the
  column expensive, so the number is asserted rather than assumed.
"""

import psycopg
import pytest

from tests.db.conftest import TwoTenants
from tests.db.factories import (
    an_embedding,
    create_agent_version,
    create_knowledge_chunk,
    create_llm_call,
)


class TestOnlyOneVersionCanBeActive:
    def test_a_second_active_version_for_the_same_tenant_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        create_agent_version(admin, two_tenants.a.id, status="active")

        with pytest.raises(psycopg.errors.UniqueViolation):
            create_agent_version(admin, two_tenants.a.id, status="active")

    def test_each_tenant_keeps_its_own_active_version(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The index is partial AND per tenant: A being active says nothing about B.
        create_agent_version(admin, two_tenants.a.id, status="active")
        b_active = create_agent_version(admin, two_tenants.b.id, status="active")

        assert b_active is not None

    def test_the_index_only_watches_active_rows(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # append-only history: the drafts and the archived ones pile up freely,
        # otherwise the version tree could never grow past two rows.
        for _ in range(3):
            create_agent_version(admin, two_tenants.a.id, status="draft")
        for _ in range(2):
            create_agent_version(admin, two_tenants.a.id, status="archived")

        rows = admin.execute(
            "select count(*) from public.agent_versions where tenant_id = %s",
            (two_tenants.a.id,),
        ).fetchone()

        assert rows[0] == 5


class TestTheModelIsPerTenantConfiguration:
    def test_a_version_without_an_explicit_model_gets_the_canonical_default(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        version_id = create_agent_version(admin, two_tenants.a.id)

        model = admin.execute(
            "select model from public.agent_versions where id = %s", (version_id,)
        ).fetchone()[0]

        assert model == "claude-sonnet-5"

    def test_two_tenants_can_run_different_models(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The whole point of D1 as amended: swapping a tenant's model is an
        # UPDATE, never a deploy.
        a = create_agent_version(admin, two_tenants.a.id, model="claude-opus-5")
        b = create_agent_version(admin, two_tenants.b.id, model="claude-haiku-4-5")

        models = admin.execute(
            "select id, model from public.agent_versions where id = any(%s) order by model",
            ([a, b],),
        ).fetchall()

        assert [row[1] for row in models] == ["claude-haiku-4-5", "claude-opus-5"]

    def test_an_empty_model_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # "" would route to no provider at all and only fail at call time, far
        # from the config screen that caused it.
        with pytest.raises(psycopg.errors.CheckViolation):
            create_agent_version(admin, two_tenants.a.id, model="")


class TestTheEmbeddingDimensionIsFrozen:
    def test_a_chunk_with_1536_dimensions_is_stored(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        chunk_id = create_knowledge_chunk(admin, two_tenants.a.id)

        assert chunk_id is not None

    def test_a_vector_of_another_dimension_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # text-embedding-3-small at 1536 (D2). A column typed `vector` with no
        # dimension would swallow this and poison the index later.
        with pytest.raises(psycopg.Error, match="1536"):
            create_knowledge_chunk(admin, two_tenants.a.id, embedding=an_embedding(768))


class TestTheLlmCallKeepsTheRoutingTrail:
    def test_provider_and_model_are_recorded(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Every call goes out through OpenRouter and the model varies per tenant
        # (D1). Without both columns the cost trail cannot say what was billed.
        call_id = create_llm_call(admin, two_tenants.a.id, model="claude-sonnet-5")

        row = admin.execute(
            "select provider, model from internal.llm_calls where id = %s", (call_id,)
        ).fetchone()

        assert row == ("openrouter", "claude-sonnet-5")
