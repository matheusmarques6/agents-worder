"""The quota enforcement point — RF-073, decision D9 of the E3 plan.

There is no plan table and no plan rule yet, and `quota_rules` is deliberately
NOT being created: a placeholder table nobody tests is worse than no table. What
is born now is the *call*, inside the protection ladder, plus a test asserting
that the call is made — because an enforcement point nobody exercises rots in
silence until the day plans exist and someone discovers the hook was never
wired.

So these tests do not check a limit. They check that the ladder asks, and that
the default answer is "unlimited".
"""

from datetime import UTC, datetime, timedelta

import pytest

from agents_runtime import quota
from agents_runtime.dispatch.ladder import ProactiveTouch, decide
from agents_runtime.quota import Allowance, has_headroom
from tests.support.clock import FrozenClock

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _touch(**overrides) -> ProactiveTouch:
    facts: dict[str, object] = {"event_at": NOW - timedelta(hours=6), "inbound_seq": 7}
    facts.update(overrides)
    return ProactiveTouch(**facts)  # type: ignore[arg-type]


class TestTheDefault:
    def test_an_allowance_nobody_configured_is_unlimited(self) -> None:
        assert has_headroom(Allowance()) is True

    def test_an_explicitly_unlimited_dimension_is_unlimited(self) -> None:
        assert has_headroom(Allowance(proactive_touches_remaining=None)) is True

    def test_a_spent_allowance_has_no_headroom(self) -> None:
        assert has_headroom(Allowance(proactive_touches_remaining=0)) is False

    def test_a_negative_allowance_has_no_headroom(self) -> None:
        """Defensive: a counter that overshot is still an exhausted plan."""
        assert has_headroom(Allowance(proactive_touches_remaining=-3)) is False


class TestTheLadderConsultsIt:
    def test_every_proactive_decision_asks_the_enforcement_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked: list[Allowance] = []

        def spy(allowance: Allowance) -> bool:
            asked.append(allowance)
            return True

        monkeypatch.setattr(quota, "has_headroom", spy)
        allowance = Allowance(proactive_touches_remaining=7)

        decide(_touch(quota=allowance), FrozenClock(NOW))

        assert asked == [allowance]

    def test_the_answer_is_obeyed_and_not_merely_collected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook whose answer is ignored is a hook that does not exist."""
        monkeypatch.setattr(quota, "has_headroom", lambda _allowance: False)

        decision = decide(_touch(), FrozenClock(NOW))

        assert decision.allow is False
        assert decision.reason == "quota_exceeded"
