"""The clock, as a dependency.

Every time-shaped rule in this system — the 10s inbound debounce, the 2min
conversation lease, the 72h gap between funnels, the 12-month retention TTL —
has to be testable without a test that waits. So domain code never reads the
wall clock itself: it receives a `Clock` and asks it.

This module is the ONE place allowed to touch `datetime.now` and
`asyncio.sleep`; `tests/unit/test_no_direct_clock.py` fails the build if a
second place appears.
"""

import asyncio
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Reading the current instant and yielding time, as an injected capability."""

    def now(self) -> datetime:
        """The current instant, always timezone-aware and in UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Yield for `seconds` of this clock's time."""
        ...


class SystemClock:
    """The production clock: real UTC, real asyncio sleep."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
