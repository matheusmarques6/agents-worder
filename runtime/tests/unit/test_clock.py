"""The injectable clock — E0-06.

Debounce (10s), staleness, cooldowns (72h), warm-up and TTL (12 months) are all
time-shaped rules. If the clock is not injectable from day one, every one of
those tests has to *wait*, and a suite that waits is a suite nobody runs. These
tests specify the contract that makes waiting unnecessary.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agents_runtime.clock import Clock, SystemClock
from tests.support.clock import FrozenClock


class TestSystemClock:
    def test_now_is_timezone_aware_and_utc(self) -> None:
        moment = SystemClock().now()

        assert moment.tzinfo is not None
        assert moment.utcoffset() == timedelta(0)

    def test_now_advances(self) -> None:
        clock = SystemClock()

        first = clock.now()
        second = clock.now()

        assert second >= first

    async def test_sleep_awaits(self) -> None:
        await SystemClock().sleep(0)

    def test_satisfies_the_clock_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)


class TestFrozenClock:
    def test_now_does_not_move_on_its_own(self) -> None:
        clock = FrozenClock(datetime(2026, 8, 2, 12, 0, tzinfo=UTC))

        assert clock.now() == clock.now()

    def test_rejects_a_naive_start(self) -> None:
        with pytest.raises(ValueError):
            FrozenClock(datetime(2026, 8, 2, 12, 0))

    def test_advance_moves_now_forward(self) -> None:
        clock = FrozenClock(datetime(2026, 8, 2, 12, 0, tzinfo=UTC))

        clock.advance(timedelta(seconds=10))

        assert clock.now() == datetime(2026, 8, 2, 12, 0, 10, tzinfo=UTC)

    async def test_sleep_advances_the_clock_without_waiting(self) -> None:
        clock = FrozenClock(datetime(2026, 8, 2, 12, 0, tzinfo=UTC))

        before_real = SystemClock().now()
        await clock.sleep(72 * 3600)  # the 72h cooldown between funnels
        elapsed_real = SystemClock().now() - before_real

        assert clock.now() == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
        assert elapsed_real < timedelta(seconds=1)

    def test_satisfies_the_clock_protocol(self) -> None:
        assert isinstance(FrozenClock(datetime(2026, 8, 2, tzinfo=UTC)), Clock)
