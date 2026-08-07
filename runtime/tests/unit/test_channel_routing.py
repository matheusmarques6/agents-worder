"""E3 · S7 — a linha da outbox sai pelo canal que ela nomeia.

Até este passo o processo tinha um canal só, então `channel_type` era um campo
que a `claimed_send` carregava e ninguém lia. Com o segundo adaptador isso deixa
de ser inofensivo: uma linha de uma conta Evolution entregue ao adaptador da
Cloud vira um POST autenticado com o token do Meta para um `phone_number_id` que
é, na verdade, o nome de uma instância. O provedor responde 4xx e a mensagem
morre — no melhor caso. No pior, o id colide com algo real.

É o mesmo princípio que o passo aplica a `funnels.channel_preference`: **config
sem consumidor mente**. `channels_accounts.type` existe desde o E1 e escolhia
nada.
"""

import uuid

import pytest

from agents_runtime.channels.port import ClaimedSend
from agents_runtime.channels.routing import CLOUD, EVOLUTION, RoutedChannels, UnroutableSend


class Spy:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[ClaimedSend] = []

    async def send(self, send: ClaimedSend) -> str:
        self.sent.append(send)
        return f"{self.name}-id"


def a_send(channel_type: str) -> ClaimedSend:
    return ClaimedSend(
        outbox_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_type=channel_type,
        channel_external_id="x",
        to_phone_e164="+5511987654321",
        payload={"text": "oi"},
        idempotency_key="k",
        attempt_count=1,
    )


async def test_each_type_reaches_its_own_adapter() -> None:
    cloud, evolution = Spy(CLOUD), Spy(EVOLUTION)
    router = RoutedChannels({CLOUD: cloud, EVOLUTION: evolution})

    assert await router.send(a_send(EVOLUTION)) == "evolution-id"
    assert await router.send(a_send(CLOUD)) == "cloud-id"

    assert [send.channel_type for send in cloud.sent] == [CLOUD]
    assert [send.channel_type for send in evolution.sent] == [EVOLUTION]


async def test_a_type_with_no_adapter_never_leaves_by_the_wrong_door() -> None:
    # Um processo configurado só com a Cloud recebendo uma linha da Evolution:
    # a resposta certa é falhar, não "entregar pelo canal que existe". Entregar
    # pelo outro é mandar a mensagem com a credencial errada para um id que
    # significa outra coisa.
    cloud = Spy(CLOUD)
    router = RoutedChannels({CLOUD: cloud})

    with pytest.raises(UnroutableSend, match=EVOLUTION):
        await router.send(a_send(EVOLUTION))

    assert cloud.sent == []


async def test_the_router_is_a_channel_port() -> None:
    # O sender recebe UM `ChannelPort` e continua recebendo um: o roteamento é
    # uma implementação da porta, não um conceito novo no caminho de envio.
    from agents_runtime.channels.port import ChannelPort

    router: ChannelPort = RoutedChannels({CLOUD: Spy(CLOUD)})

    assert await router.send(a_send(CLOUD)) == "cloud-id"
