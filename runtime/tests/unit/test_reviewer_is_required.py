"""Fitness function — the process refuses to run without the post-hoc audit.

The sibling of `test_responder_is_required`, and it exists because the reviewer
turned out to be on the responder's side of the asymmetry rather than the
channel's. The first version of S9b argued the opposite; Bruno overruled it, and
the reasons are worth keeping next to the assertion:

  * **RF-008 is a promise to the merchant, not an internal metric.** Every new
    tenant enters a shadow window with 100% of its replies evaluated. A process
    that boots without `AGENTS_REVIEWER` honours Judge 1 and breaks that promise
    in SILENCE — `q_evals` is not even polled, so the jobs pile up unread;
  * **the queue-depth alert that would make that noisy does not exist until
    E6.** "It will be visible" is a claim about a future milestone, not about
    the deploy that ships today;
  * **the correction is the ONLY repair path** for a violation that already
    reached a customer. Without a reviewer it never happens, and nobody learns
    that it did not.

Refusing to start is a high, safe failure — ADR-2 already accepts the VPS as a
single point of failure for processing. Starting and auditing nothing is the
silent lie decisão 91 exists to prevent.

`app.run` KEEPS its permissive default (`review=None`), because the E1 engine
scenarios drive it directly and Lei 1 forbids touching them. What stops existing
is the PRODUCTION path to it — exactly the shape of decisão 89.

These tests drive `_serve`, not the helper: the guard being tested is the
WIRING, so they enter through the door production enters. A responder is
configured in every one of them, because the responder's own refusal happens
first and would mask this one.
"""

import asyncio

import pytest

from agents_runtime.__main__ import RESPONDER_VARIABLE, REVIEWER_VARIABLE, _serve

UNUSED_DSN = "postgresql://nobody@127.0.0.1:1/none"

A_RESPONDER = "tests.unit.test_reviewer_is_required:_a_responder_factory"


@pytest.fixture(autouse=True)
def a_configured_responder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The responder refuses first. Without this the tests below would pass on
    the wrong refusal — the trap of decisão 89, one seam over."""
    monkeypatch.setenv(RESPONDER_VARIABLE, A_RESPONDER)


class TestTheProcessRefusesToAuditNothing:
    def test_starting_without_a_reviewer_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent must not mean "run happily and evaluate nobody"."""
        monkeypatch.delenv(REVIEWER_VARIABLE, raising=False)

        with pytest.raises(RuntimeError, match=REVIEWER_VARIABLE):
            asyncio.run(_serve(UNUSED_DSN))

    def test_starting_with_a_blank_reviewer_is_fatal_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace is how a compose file spells "I meant to set this"."""
        monkeypatch.setenv(REVIEWER_VARIABLE, "   ")

        with pytest.raises(RuntimeError, match=REVIEWER_VARIABLE):
            asyncio.run(_serve(UNUSED_DSN))

    def test_the_refusal_names_the_promise_so_the_operator_knows_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A startup failure at 3am is only useful if it says what is broken.
        Here that is the shadow window, not "an evaluation seam"."""
        monkeypatch.delenv(REVIEWER_VARIABLE, raising=False)

        with pytest.raises(RuntimeError, match="shadow"):
            asyncio.run(_serve(UNUSED_DSN))


async def _a_reviewer(job):
    return None


def _a_responder_factory(dsn: str):
    return _a_reviewer
