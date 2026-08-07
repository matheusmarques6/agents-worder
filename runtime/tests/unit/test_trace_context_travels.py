"""O `traceparent` sobrevive à fila — a metade da observabilidade que não tem rede.

O `CLAUDE.md` diz, com estas palavras: "`traceparent` viaja dentro dos payloads
de fila (coluna `otel`)". O slot existe desde o E1 e as quatro classes de job o
leem; o que nunca existiu foi um PRODUTOR que o escrevesse, e um slot que só é
lido é um slot que vai chegar vazio no dia em que o Logfire existir.

Estes testes fixam o carregamento do contexto, e só ele. Exportador, SDK e
endpoint OTLP dependem de B-2/B-3 e não estão aqui — o que está aqui é a
propriedade que, faltando, torna o exportador inútil quando chegar: um job criado
por causa de outro trabalho carrega o contexto daquele trabalho.
"""

import uuid

import pytest

from agents_runtime.obs import context
from agents_runtime.queueing.jobs import ScheduledTouchJob

pytestmark = pytest.mark.unit


class TestOCarrierEhOFormatoW3C:
    def test_sem_contexto_nao_inventa_nenhum(self) -> None:
        # O default do produto hoje. `None` e não `{}`: um dicionário vazio no
        # payload diria "houve um trace e ele estava vazio", que é uma afirmação
        # diferente de "ninguém instrumentou isto ainda".
        assert context.no_trace_context() is None

    def test_um_traceparent_valido_vira_carrier(self) -> None:
        parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        assert context.carrier(parent) == {"traceparent": parent}

    def test_o_tracestate_acompanha_quando_existe(self) -> None:
        parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        assert context.carrier(parent, tracestate="rojo=00f067aa0ba902b7") == {
            "traceparent": parent,
            "tracestate": "rojo=00f067aa0ba902b7",
        }

    @pytest.mark.parametrize(
        "invalid",
        [
            "",
            "não é um traceparent",
            # trace-id todo zero: o W3C manda descartar.
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
            # span-id todo zero, idem.
            "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
            # versão `ff` é reservada.
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            # campos a menos.
            "00-4bf92f3577b34da6a3ce929d0e0e4736-01",
        ],
    )
    def test_um_traceparent_malformado_nao_vira_carrier(self, invalid: str) -> None:
        # Fail closed, e a razão é a mesma do resto do repositório: um contexto
        # inválido propagado é um trace que se parte em dois no backend sem que
        # nada avise. Sem contexto é um fato; contexto errado é uma mentira.
        assert context.carrier(invalid) is None


class TestOJobDeToqueCarregaOContexto:
    def test_o_payload_leva_o_otel_quando_ha_contexto(self) -> None:
        parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        job = ScheduledTouchJob(
            scheduled_touch_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            otel=context.carrier(parent),
        )

        assert job.to_payload()["otel"] == {"traceparent": parent}

    def test_a_ida_e_a_volta_preservam_o_contexto(self) -> None:
        # A varredura produz e o handler consome, ambos em Python, neste mesmo
        # processo: se a volta perdesse o contexto, o span do toque nasceria
        # órfão do tique que o criou.
        job = ScheduledTouchJob(
            scheduled_touch_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            otel=context.carrier("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
        )

        assert ScheduledTouchJob.from_payload(job.to_payload()) == job

    def test_sem_contexto_o_payload_continua_sendo_o_de_antes(self) -> None:
        # A trava do S4 (`test_no_fact_travels_in_the_payload`) diz que só ids
        # viajam. Um `"otel": null` fixo passaria por ela e sujaria a fila com uma
        # chave que não afirma nada — então a chave só existe quando há o que
        # carregar.
        job = ScheduledTouchJob(scheduled_touch_id=uuid.uuid4(), tenant_id=uuid.uuid4())

        assert set(job.to_payload()) == {"scheduled_touch_id", "tenant_id"}
