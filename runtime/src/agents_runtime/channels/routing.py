"""Two channels, one port — which door this send leaves by.

`ClaimedSend.channel_type` is `channels_accounts.type`, and until the second
adapter existed nothing read it. With two adapters in the process that stops
being harmless: an Evolution row handed to the Cloud adapter is a POST
authenticated with the Meta token, to a `phone_number_id` that is really an
instance name.

This is an implementation of `ChannelPort`, not a new concept in the send path —
the sender still receives exactly one port, and still knows nothing about who is
behind it (ADR-8).

A type with no adapter RAISES rather than falling back to whichever adapter the
process happens to hold. A fallback here would be the same class of mistake as a
`channel_preference` that silently routes elsewhere: configuration that lies.
"""

from agents_runtime.channels.port import ChannelPort, ClaimedSend

#: `channels_accounts.type`, the two values its CHECK allows.
CLOUD = "cloud"
EVOLUTION = "evolution"


class UnroutableSend(RuntimeError):
    """This process has no adapter for the channel this row belongs to.

    A configuration fault, not a provider failure. `LookupError` would classify
    as permanent and give up on the message; this is `RuntimeError` so the
    classifier's UNKNOWN path retries it — a sender started with the wrong
    channel set is usually a deploy away from being right, and the outbox row is
    a real message somebody is waiting for. The queue's retry limit still ends
    the argument.
    """


class RoutedChannels:
    def __init__(self, adapters: dict[str, ChannelPort]) -> None:
        self._adapters = dict(adapters)

    async def send(self, send: ClaimedSend) -> str:
        adapter = self._adapters.get(send.channel_type)
        if adapter is None:
            raise UnroutableSend(
                f"nenhum adaptador para channel_type={send.channel_type!r} — "
                f"este processo carrega {sorted(self._adapters)}"
            )
        return await adapter.send(send)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()
