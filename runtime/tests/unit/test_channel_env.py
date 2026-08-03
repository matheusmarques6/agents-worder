"""The channel factory dies loudly — the review's one-line doubt, resolved.

`AGENTS_CHANNEL` absent means no sender, explicitly. But absent and BROKEN are
different states: a typo in the factory path must kill the process at startup,
not quietly become "no sender in production". Characterization tests — the
behaviour already existed; these pin it so a refactor cannot soften it into
silence.
"""

import pytest

from agents_runtime.__main__ import _channel_from_env


def test_absent_means_no_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTS_CHANNEL", raising=False)

    assert _channel_from_env("postgresql://x") is None


def test_a_missing_module_dies_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS_CHANNEL", "no.such.module:create")

    with pytest.raises(ModuleNotFoundError):
        _channel_from_env("postgresql://x")


def test_a_missing_attribute_dies_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTS_CHANNEL", "agents_runtime.config:no_such_factory")

    with pytest.raises(AttributeError):
        _channel_from_env("postgresql://x")


def test_a_spec_without_a_colon_dies_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    # "module" with no ":callable" partitions into an empty attribute name —
    # getattr('') can never succeed, so the typo is caught at the door.
    monkeypatch.setenv("AGENTS_CHANNEL", "agents_runtime.config")

    with pytest.raises(AttributeError):
        _channel_from_env("postgresql://x")
