"""Unidade 4 — transitório e permanente.

A classificação é o que decide entre "tenta de novo" e "vai para a DLQ". Errar
para o lado transitório queima cinco tentativas num payload que nunca vai
funcionar; errar para o permanente joga fora uma mensagem que só precisava
esperar o 429 passar.

O caso não mapeado tem decisão explícita, e ela está registrada no módulo: erro
desconhecido é tratado como transitório **mas o limite de tentativas continua
valendo**. Sem o limite, "transitório" viraria "para sempre" e a DLQ, letra
morta.
"""

import pytest

from agents_runtime.queueing.failures import Failure, classify


class ApiTimeout(TimeoutError):
    pass


@pytest.mark.parametrize(
    "error",
    [
        ApiTimeout("o provedor não respondeu"),
        ConnectionError("conexão caiu"),
        # 429 e 5xx chegam como erro de status do adaptador do canal.
        RuntimeError("HTTP 429 Too Many Requests"),
        RuntimeError("HTTP 503 Service Unavailable"),
        RuntimeError("deadlock detected"),
    ],
)
def test_what_may_work_next_time_is_transient(error: Exception) -> None:
    assert classify(error) is Failure.TRANSIENT


@pytest.mark.parametrize(
    "error",
    [
        ValueError("payload inválido: falta o campo 'from'"),
        PermissionError("credencial revogada"),
        LookupError("tenant inexistente"),
        RuntimeError("HTTP 400 Bad Request"),
        RuntimeError("HTTP 401 Unauthorized"),
    ],
)
def test_what_will_never_work_is_permanent(error: Exception) -> None:
    assert classify(error) is Failure.PERMANENT


def test_an_unmapped_error_is_flagged_rather_than_guessed() -> None:
    # UNKNOWN não é um terceiro comportamento: ele repete como transitório e
    # morre no limite. O valor de tê-lo separado é que a métrica de UNKNOWN
    # subindo diz que a tabela de classificação envelheceu.
    class SomethingNew(Exception):
        pass

    assert classify(SomethingNew("nunca vi isso")) is Failure.UNKNOWN


def test_the_status_code_wins_over_the_exception_type() -> None:
    # Um 400 embrulhado em RuntimeError é permanente, e um 500 no mesmo tipo é
    # transitório — o que decide é o que o provedor respondeu.
    assert classify(RuntimeError("HTTP 500")) is Failure.TRANSIENT
    assert classify(RuntimeError("HTTP 422 Unprocessable Entity")) is Failure.PERMANENT
