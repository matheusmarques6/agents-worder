"""E3 · S7 — o adaptador da Evolution, contra transporte falso.

Mesmo molde do `test_cloud_api_channel.py` do E1, e pela mesma razão:
`httpx.MockTransport` mantém tudo em processo, nenhum socket é aberto, e o teste
é honestamente `unit`. O que ele NÃO prova é o lado da Evolution — e na Evolution
essa lista é maior que na Cloud, porque não existe fornecedor: existe a instância
que o lojista (ou nós) subimos. O que só a suíte `contract` poderá confirmar está
escrito no docstring do adaptador, item por item.

R1 do plano: nenhum teste bloqueante toca a rede, e a instância real só entra na
`contract`, sob demanda, já com o warm-up ligado. Banir o número de teste durante
o desenvolvimento deste passo é o risco nº 1 do marco.
"""

import json
import uuid

import httpx
import pytest

from agents_runtime.channels.evolution import EvolutionChannel, from_env
from agents_runtime.channels.port import ClaimedSend
from agents_runtime.dispatch import consent
from agents_runtime.queueing.failures import Failure, classify

BASE_URL = "http://localhost:8080"


def a_send(**overrides) -> ClaimedSend:
    defaults = dict(
        outbox_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_type="evolution",
        # Na Evolution o "external id" é o nome da INSTÂNCIA, não um id numérico
        # de telefone: é assim que a API roteia, pelo path.
        channel_external_id="loja-teste",
        to_phone_e164="+5511987654321",
        payload={"text": "Seu pedido saiu para entrega 🧡", "generated": False},
        idempotency_key="touch-abc-2",
        attempt_count=1,
    )
    return ClaimedSend(**{**defaults, **overrides})


def channel_answering(handler) -> EvolutionChannel:
    return EvolutionChannel(BASE_URL, "apikey-de-teste", transport=httpx.MockTransport(handler))


def accepted(message_id: str = "3EB0C767D097B7C7A0A6") -> httpx.Response:
    """A resposta que a Evolution devolve quando aceita um texto."""
    return httpx.Response(
        201, json={"key": {"id": message_id, "fromMe": True}, "status": "PENDING"}
    )


class TestTheRequestIsTheOneEvolutionExpects:
    async def test_the_instance_routes_in_the_path_and_the_key_in_the_header(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["apikey"] = request.headers.get("apikey")
            seen["body"] = json.loads(request.content)
            return accepted()

        message_id = await channel_answering(handler).send(a_send())

        assert message_id == "3EB0C767D097B7C7A0A6"
        assert seen["url"].endswith("/message/sendText/loja-teste")
        # `apikey`, não `Authorization: Bearer`. Duas APIs, dois esquemas — e o
        # adaptador existe justamente para que nada além dele saiba disso.
        assert seen["apikey"] == "apikey-de-teste"
        assert seen["body"]["text"] == "Seu pedido saiu para entrega 🧡"

    async def test_the_plus_of_e164_does_not_travel(self) -> None:
        # A Evolution endereça por JID, e o JID não tem `+`. Mandar `+55…` faz a
        # mensagem ir para um número que não existe — e a API responde 200,
        # porque para ela o destino é só uma string.
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return accepted()

        await channel_answering(handler).send(a_send())

        assert seen["number"] == "5511987654321"

    async def test_only_the_text_the_dispatch_decided_is_sent(self) -> None:
        # D10: conteúdo é do dispatch. O `generated` viaja no payload da outbox
        # para a auditoria (D3c) e NÃO é assunto do provedor — um adaptador que
        # repassasse a nossa contabilidade interna para fora seria um adaptador
        # vazando o nosso modelo de dados no corpo de uma mensagem.
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return accepted()

        await channel_answering(handler).send(a_send(payload={"text": "oi", "generated": True}))

        assert "generated" not in seen


class TestWhatNeverReachesTheWire:
    async def test_a_payload_without_text_is_refused(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return accepted()

        with pytest.raises(ValueError):
            await channel_answering(handler).send(a_send(payload={"template_ref": "x"}))

        assert calls == 0, "adivinhar uma forma desconhecida manda ALGUMA COISA para um cliente"

    async def test_a_touch_carrying_the_consent_buttons_is_refused(self) -> None:
        # A decisão mais desconfortável deste passo, e ela é conservadora de
        # propósito.
        #
        # O RF-033(a) manda TODO toque a contato não consentido carregar o par
        # Autorizar/Bloquear. Na Cloud isso é `interactive.button`, uma forma
        # documentada, e a resposta volta em `button_reply.id` — que é o que
        # `dispatch/consent.py` reconhece deterministicamente. Na Evolution não
        # existe nenhuma das duas pontas: a forma de botão do Baileys não é
        # confiável no WhatsApp atual, e a ingestão da Evolution (que traduziria
        # a resposta) não existe ainda.
        #
        # As três saídas eram: adivinhar a forma do botão; mandar o texto SEM a
        # escolha; ou recusar. Adivinhar contraria a doutrina que o próprio
        # adaptador da Cloud escreveu ("nunca adivinhe um botão: ou você derruba
        # o jeito da pessoa recusar, ou manda um controle que ela não consegue
        # usar"). Mandar sem a escolha é pior: seria vender para quem nunca
        # consentiu e chamar isso de sucesso, com a linha da outbox marcada
        # `sent`. Recusar é a única das três que **suprime** um envio em vez de
        # criar um — e falha alto, em `failed`, onde um humano vê.
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return accepted()

        touch = a_send(
            payload={
                "text": "Vi que ficou algo no carrinho.",
                consent.BUTTONS: consent.buttons_for(consent.PENDING),
            }
        )
        with pytest.raises(ValueError) as refusal:
            await channel_answering(handler).send(touch)

        assert calls == 0
        assert classify(refusal.value) is Failure.PERMANENT


class TestTheProviderSpeaksTheClassifiersLanguage:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, Failure.TRANSIENT),
            (500, Failure.TRANSIENT),
            (502, Failure.TRANSIENT),
            (400, Failure.PERMANENT),
            (401, Failure.PERMANENT),
            (404, Failure.PERMANENT),
        ],
    )
    async def test_the_status_is_readable_in_the_error(
        self, status: int, expected: Failure
    ) -> None:
        # A mensagem de erro do adaptador É o contrato dele com a unidade 4. Dois
        # adaptadores, um classificador, um vocabulário.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"message": "nope"})

        with pytest.raises(RuntimeError) as failure:
            await channel_answering(handler).send(a_send())

        assert classify(failure.value) is expected

    async def test_a_2xx_without_a_message_id_is_permanent(self) -> None:
        # Igual à Cloud: um sucesso que o provedor não sabe nomear é violação de
        # contrato. Reenviar às cegas é como um cliente recebe a mesma mensagem
        # duas vezes.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"status": "PENDING"})

        with pytest.raises(ValueError) as failure:
            await channel_answering(handler).send(a_send())

        assert classify(failure.value) is Failure.PERMANENT

    async def test_an_empty_key_object_is_not_a_message_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"key": {}})

        with pytest.raises(ValueError):
            await channel_answering(handler).send(a_send())


class TestTheFactoryDiesLoudly:
    def test_a_missing_base_url_dies_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Evolution não tem endereço de fornecedor: o host é a instância que
        # alguém subiu. Por isso a URL é OBRIGATÓRIA e não tem default — um
        # default aqui seria um palpite sobre a infraestrutura de outra pessoa.
        monkeypatch.delenv("AGENTS_EVOLUTION_BASE_URL", raising=False)
        monkeypatch.setenv("AGENTS_EVOLUTION_API_KEY", "k")

        with pytest.raises(RuntimeError, match="AGENTS_EVOLUTION_BASE_URL"):
            from_env("postgresql://x")

    def test_a_missing_api_key_dies_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTS_EVOLUTION_BASE_URL", BASE_URL)
        monkeypatch.delenv("AGENTS_EVOLUTION_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="AGENTS_EVOLUTION_API_KEY"):
            from_env("postgresql://x")
