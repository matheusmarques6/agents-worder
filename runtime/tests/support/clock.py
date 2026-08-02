"""A clock the tests drive by hand.

`sleep` moves the clock instead of the wall: a test can cross the 72h cooldown
in microseconds. Lives under `tests/` on purpose — a test double has no
business shipping inside the runtime package.
"""

from datetime import datetime, timedelta


class FrozenClock:
    """A clock that only moves when a test tells it to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware start instant")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    async def sleep(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))
