"""The connector seam, read from the environment — absent and broken differ.

Same doctrine as `test_channel_env.py` (decisão 67): a seam that is UNSET is a
deliberate configuration, and a seam that is MISCONFIGURED is a mistake. The
two must not produce the same behaviour, because the whole point of a startup
failure is that a human is watching when it happens.

For connectors, absent is the safe side: the webhook is still the primary path
and the sweep simply does not run. Misconfigured is not safe, and specifically
not safe in a way nothing downstream reveals — a typo in a platform name
produces a sweep that claims that merchant's stores every tick and closes them
all `error`, which reads in the hub exactly like a broken integration on the
merchant's side.
"""

import pytest

from agents_runtime.__main__ import _connectors_from_env
from agents_runtime.connectors.shopify import ShopifyConnector

pytestmark = pytest.mark.unit

DSN = "postgresql://postgres@localhost:5432/postgres"
SPEC = "agents_runtime.connectors.shopify:from_env"


class TestAbsentIsAConfiguration:
    def test_unset_means_no_sweep_rather_than_an_empty_sweep(self, monkeypatch) -> None:
        monkeypatch.delenv("AGENTS_CONNECTORS", raising=False)
        assert _connectors_from_env(DSN) == {}

    def test_an_empty_value_is_the_same_as_unset(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTS_CONNECTORS", "   ")
        assert _connectors_from_env(DSN) == {}


class TestBrokenIsNotAbsent:
    def test_a_pair_without_a_platform_dies_at_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTS_CONNECTORS", SPEC)
        with pytest.raises(RuntimeError, match="malformado"):
            _connectors_from_env(DSN)

    def test_a_platform_without_a_factory_dies_at_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTS_CONNECTORS", "shopify=")
        with pytest.raises(RuntimeError, match="malformado"):
            _connectors_from_env(DSN)

    def test_a_module_that_does_not_import_dies_at_startup(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTS_CONNECTORS", "shopify=nao.existe:from_env")
        with pytest.raises(ModuleNotFoundError):
            _connectors_from_env(DSN)


class TestTheMapIsKeyedByPlatform:
    def test_one_entry_builds_one_adapter(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENTS_CONNECTORS", f"shopify={SPEC}")
        connectors = _connectors_from_env(DSN)

        # The key has to be exactly `connector_accounts.platform`: the sweep
        # looks the adapter up by that string, and a mismatch is not an error
        # anywhere — it is a store closed `error` every tick forever.
        assert set(connectors) == {"shopify"}
        assert isinstance(connectors["shopify"], ShopifyConnector)

    def test_several_platforms_share_one_sweep(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "AGENTS_CONNECTORS", f" shopify={SPEC} , nuvemshop={SPEC} "
        )
        assert set(_connectors_from_env(DSN)) == {"shopify", "nuvemshop"}
