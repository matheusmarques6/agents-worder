"""`funnels.touches` stops being free-form jsonb — dívida (b) do S3.

The cadence is read by `internal.start_funnel_run` (which casts `delay` straight
to `interval`) and by `dispatch/copy.py` (which looks the touch up by `n`). Until
this migration, neither had anything to trust: a merchant could save a cadence
with no `delay`, with the same `n` twice, or with nothing in it at all, and the
three failures are three different kinds of bad —

  * no `delay` raises inside `start_funnel_run`, at whatever hour the
    abandonment happens to arrive;
  * a duplicate `n` schedules two touches the dispatcher cannot tell apart, and
    the unique index of S3 rejects the second half of the cadence at insert;
  * an empty cadence is the worst of the three, because there is no error at
    all: the funnel is switched on and silently does nothing.

Risk R5 of the plan is the rule this constraint writes down: `touches` is a list
of `{n, delay, copy_base[, template_ref, cta]}` and nothing else. A conditional
inside a funnel is a rule that wanted to be code.
"""

import psycopg
import pytest

from tests.db.conftest import TwoTenants
from tests.db.factories_e3 import create_funnel

pytestmark = pytest.mark.db


def _cadence(admin: psycopg.Connection, tenant_id, touches: list[dict]):
    return create_funnel(admin, tenant_id, touches=touches)


class TestACadenceMustBeACadence:
    def test_a_touch_without_a_delay_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            _cadence(admin, two_tenants.a.id, [{"n": 1, "copy_base": "Oi"}])

    def test_a_delay_postgres_cannot_parse_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `start_funnel_run` casts this to `interval` with no translation layer
        # in between. A shape that does not cast is a cadence that explodes at
        # scheduling time, in a function nobody is watching.
        with pytest.raises(psycopg.errors.CheckViolation):
            _cadence(admin, two_tenants.a.id, [{"n": 1, "delay": "amanhã", "copy_base": "Oi"}])

    def test_a_truncated_iso_duration_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        for broken in ("P", "PT", "P1DT"):
            with pytest.raises(psycopg.errors.CheckViolation):
                _cadence(admin, two_tenants.a.id, [{"n": 1, "delay": broken, "copy_base": "Oi"}])

    def test_the_same_touch_number_twice_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            _cadence(
                admin,
                two_tenants.a.id,
                [
                    {"n": 1, "delay": "PT0S", "copy_base": "Oi"},
                    {"n": 1, "delay": "PT6H", "copy_base": "Oi de novo"},
                ],
            )

    def test_a_touch_number_that_is_not_a_positive_integer_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `scheduled_touches.touch_number` is `integer > 0`; anything else is a
        # cadence that cannot be materialised.
        for bad in (0, -1, 1.5, "1"):
            with pytest.raises(psycopg.errors.CheckViolation):
                _cadence(admin, two_tenants.a.id, [{"n": bad, "delay": "PT0S", "copy_base": "Oi"}])

    def test_an_empty_cadence_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `enabled` is the switch. An empty list is an accident, and it is the
        # one failure with no symptom.
        with pytest.raises(psycopg.errors.CheckViolation):
            _cadence(admin, two_tenants.a.id, [])

    def test_a_touch_with_nothing_to_say_is_rejected(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # Text is the one thing every adapter can deliver today — the Cloud API
        # adapter rejects an outbox payload without it. A cadence entry naming
        # only a template is a touch nothing can send, and finding that out in
        # the sender means the touch was already committed to the outbox.
        with pytest.raises(psycopg.errors.CheckViolation):
            _cadence(
                admin, two_tenants.a.id, [{"n": 1, "delay": "PT0S", "template_ref": "cart_v1"}]
            )

    def test_omitting_the_cadence_entirely_fails_by_naming_the_column(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # The default of `'[]'` was dropped with this constraint: it was the one
        # value the CHECK now rejects, so leaving it would turn "I forgot the
        # cadence" into a CHECK violation on a column the writer never mentioned.
        with pytest.raises(psycopg.errors.NotNullViolation):
            admin.execute(
                "insert into public.funnels (tenant_id, occasion) values (%s, 'cart_abandoned')",
                (two_tenants.a.id,),
            )


class TestTheCadencesTheProductActuallyUses:
    def test_the_canonical_two_touch_cadence_is_accepted(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        funnel = _cadence(
            admin,
            two_tenants.a.id,
            [
                {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo no carrinho."},
                {"n": 2, "delay": "PT24H", "copy_base": "Ainda dá tempo.", "cta": "Finalizar"},
            ],
        )

        assert funnel.id is not None

    def test_a_template_reference_alongside_the_text_is_accepted(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        funnel = _cadence(
            admin,
            two_tenants.a.id,
            [{"n": 1, "delay": "P1DT6H", "copy_base": "Oi", "template_ref": "cart_v1"}],
        )

        assert funnel.id is not None

    def test_the_cadence_may_be_saved_out_of_order(
        self, admin: psycopg.Connection, two_tenants: TwoTenants
    ) -> None:
        # `start_funnel_run` orders by `n` itself; the list's order is not the
        # cadence's order, and rejecting it would be strictness with no reason.
        funnel = _cadence(
            admin,
            two_tenants.a.id,
            [
                {"n": 2, "delay": "PT24H", "copy_base": "Ainda dá tempo."},
                {"n": 1, "delay": "PT0S", "copy_base": "Vi que ficou algo."},
            ],
        )

        assert funnel.id is not None
