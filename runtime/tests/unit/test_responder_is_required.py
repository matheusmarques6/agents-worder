"""Fitness function — the process refuses to run without a real responder.

The strongest invariant in CLAUDE.md is written without an escape hatch:

    Every agent response passes Judge 1 before sending — no exceptions,
    including load tests.

`fixed_responder` predates Judge 1: in E1 the constant reply WAS the product,
and there was no gate for it to skip. From S8 onwards there is one, and from
S9a the real responder runs it — so the constant reply became the one way an
answer reaches a customer without ever being judged.

The gap was never in the responder. It was in how the process picks one:
`_factory_from_env` returns None when the variable is absent, and `app.run`
falls back to `fixed_responder()`. One forgotten line in the VPS compose file
and the runtime boots healthy, answers every customer with a canned sentence,
writes no `judge_scores`, and raises no alert. Nothing fails — which is what
makes it worse than a crash.

So the entrypoint refuses, the way configuring a channel without a token
already refuses (decisão 67). `app.run` keeps its default, because the engine
scenarios of E1 depend on it and Lei 1 forbids touching them: what changes is
that the PRODUCTION path can no longer reach it.

**These tests drive `_serve`, not the helper.** The first version of this file
called `_factory_from_env(..., required=True)` directly and passed while the
call site inside `_serve` was sabotaged back to the old behaviour — the test
was asserting its own argument. The guard being tested is the WIRING, so the
tests enter through the same door production does. `_serve` never reaches the
database here: the arguments to `run(...)` are evaluated before the call, and
the refusal happens there.

`AGENTS_CHANNEL` is deliberately NOT covered by this rule. Without a channel
nothing is sent at all, so absent is safe there — the asymmetry is the point,
and the last test pins it so a future tidy-up cannot "make them consistent".
"""

import asyncio

import pytest

from agents_runtime.__main__ import RESPONDER_VARIABLE, _factory_from_env, _serve

UNUSED_DSN = "postgresql://nobody@127.0.0.1:1/none"


class TestTheProcessRefusesAnUnjudgedResponder:
    def test_starting_without_a_responder_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent must not mean "answer everyone with a canned line"."""
        monkeypatch.delenv(RESPONDER_VARIABLE, raising=False)

        with pytest.raises(RuntimeError, match=RESPONDER_VARIABLE):
            asyncio.run(_serve(UNUSED_DSN))

    def test_starting_with_a_blank_responder_is_fatal_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace is how a compose file spells "I meant to set this"."""
        monkeypatch.setenv(RESPONDER_VARIABLE, "   ")

        with pytest.raises(RuntimeError, match=RESPONDER_VARIABLE):
            asyncio.run(_serve(UNUSED_DSN))

    def test_the_refusal_names_the_judge_so_the_operator_knows_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A startup failure at 3am is only useful if it says what to fix."""
        monkeypatch.delenv(RESPONDER_VARIABLE, raising=False)

        with pytest.raises(RuntimeError, match="Judge 1"):
            asyncio.run(_serve(UNUSED_DSN))

    def test_a_configured_responder_is_built(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard must not break the path it is guarding."""
        monkeypatch.setenv(
            RESPONDER_VARIABLE, "tests.unit.test_responder_is_required:_a_responder_factory"
        )

        built = _factory_from_env(RESPONDER_VARIABLE, UNUSED_DSN, required=True)

        assert built is _sentinel_responder


class TestTheChannelKeepsItsOwnRule:
    def test_an_absent_channel_is_not_fatal(self) -> None:
        """Without a channel nothing is sent, so absent is safe — unlike the
        responder, where absent means "reply without judging"."""
        assert _factory_from_env("AGENTS_CHANNEL_THAT_IS_NOT_SET", UNUSED_DSN) is None


async def _sentinel_responder(job):
    return None


def _a_responder_factory(dsn: str):
    return _sentinel_responder
