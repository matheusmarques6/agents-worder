"""Where a store's token comes from — and the seam that is honestly empty.

The architecture says platform tokens live in Vault and are reached only
through scoped functions: `get_connector_secret()`, which is **E0-22 and does
not exist yet**. This module is the shape of the hole, not a substitute for it.

Nothing here invents a credential. `VaultConnectorSecrets` is written, named,
and raises — because the alternative shapes are both worse than a hole:

  * an adapter that reads a token straight out of the environment in production
    would be the trust boundary crossed by a default, and it would work, which
    is how it would survive;
  * no seam at all would mean the adapter is not finished, and "finished except
    for credentials" is a different, checkable state.

`SingleTokenSecrets` exists for exactly one caller: the `contract` suite, one
store, one token, from the environment, never blocking. It mirrors
`channels/cloud_api.py:from_env` — an adapter that has been ready for two
milestones waiting only for a token.
"""

import os
from typing import Protocol

from agents_runtime.connectors.port import SyncTarget


class ConnectorSecretUnavailable(RuntimeError):
    """No credential for this store — the pass records it and moves on.

    A distinct exception rather than a bare `RuntimeError` so the sweep's
    `sync_status = 'error'` for "we have no token" reads differently from
    "the platform is down", which is the difference between a task for
    onboarding and a task for whoever watches the platform.
    """


class ConnectorSecrets(Protocol):
    """The store's access token. One argument, and it is a row, not a string.

    `SyncTarget` and not `tenant_id`: the caller may not choose whose token it
    gets by passing an id — it gets the token of the store it was HANDED by the
    claim. Same reading as `tenant_id` never coming from a client.
    """

    async def token_for(self, target: SyncTarget) -> str: ...


class VaultConnectorSecrets:
    """The production path — **pending E0-22**, and it says so instead of guessing."""

    async def token_for(self, target: SyncTarget) -> str:
        raise ConnectorSecretUnavailable(
            f"sem credencial para {target.platform}/{target.source_account_id}: "
            "get_connector_secret() (E0-22) ainda não existe. O adaptador está "
            "pronto e a costura é esta — nenhum token é inventado aqui."
        )


class SingleTokenSecrets:
    """One store, one token — the `contract` suite's seam and nothing else.

    It refuses to answer for any store but the one it was built for. Handing the
    same token to a second store would be one tenant's credential used against
    another's data, which is the exact failure Vault-per-tenant exists to make
    impossible; a single-token stand-in that did not check would teach the
    codebase that the check is optional.
    """

    def __init__(self, *, source_account_id: str, token: str) -> None:
        self._source_account_id = source_account_id
        self._token = token

    async def token_for(self, target: SyncTarget) -> str:
        if target.source_account_id != self._source_account_id:
            raise ConnectorSecretUnavailable(
                f"este segredo é da loja {self._source_account_id}, não de "
                f"{target.source_account_id}"
            )
        return self._token


def single_token_from_env() -> SingleTokenSecrets:
    """The `contract` suite's factory. Missing credentials die loudly, never quietly."""
    shop = os.environ.get("AGENTS_SHOPIFY_SHOP")
    token = os.environ.get("AGENTS_SHOPIFY_ACCESS_TOKEN")
    if not shop or not token:
        raise ConnectorSecretUnavailable(
            "AGENTS_SHOPIFY_SHOP e AGENTS_SHOPIFY_ACCESS_TOKEN não estão definidos — "
            "a loja de desenvolvimento do B-4 ainda não chegou."
        )
    return SingleTokenSecrets(source_account_id=shop, token=token)
