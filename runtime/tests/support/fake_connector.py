"""The connector double — a platform that behaves, and one that dies mid-page.

The real adapters reach a store over HTTP; this one reads a list. What it will
NOT do is behave better than a platform: it honours the port's contract to the
letter (ascending `occurred_at`, inclusive lower bound, `limit` bounding one
pass) precisely so that a test passing against it is a test that would pass
against Shopify.

`fail_after` is the whole reason a double exists at all. "A poll that fails
halfway skips no event" cannot be asserted against a provider that has no way
to be told to fail halfway. It counts SUCCESSFUL fetches before the platform
stops answering, because that is where a bounded pass actually breaks: one pass
takes one page, and the store whose history is longer than a page comes back
next tick — from a cursor that only ever moved as far as an ingested event.
"""

from collections.abc import Sequence

from agents_runtime.connectors.port import PlatformEvent, SyncTarget


class ScriptedConnector:
    """A platform whose history is a list, ordered oldest first."""

    def __init__(
        self,
        events: Sequence[PlatformEvent],
        *,
        fail_after: int | None = None,
    ) -> None:
        self._events = sorted(events, key=lambda event: event.occurred_at)
        #: How many pages this platform hands over before it stops answering.
        #: `None` is a healthy platform; `0` is one that is down right now.
        self.fail_after = fail_after
        #: Every target this connector was asked about, in order — what proves
        #: a tick polled the accounts it claimed to poll.
        self.calls: list[SyncTarget] = []

    async def fetch_since(self, target: SyncTarget, *, limit: int) -> Sequence[PlatformEvent]:
        self.calls.append(target)
        if self.fail_after is not None:
            if self.fail_after <= 0:
                raise ConnectionError("HTTP 503 scripted connector told to fail")
            self.fail_after -= 1

        window = [
            event
            for event in self._events
            # `>=`, not `>`: an event exactly at the cursor comes back, which is
            # what makes two events in the same second safe. The duplicate it
            # costs is one row `ingest_webhook` refuses — the D5 dividend.
            if target.cursor is None or event.occurred_at >= target.cursor
        ]
        return window[:limit]

    def only_visible_to_the_poll(self, event: PlatformEvent) -> None:
        """Add a fact the webhook never delivered — a lost delivery, made real."""
        self._events = sorted([*self._events, event], key=lambda item: item.occurred_at)
