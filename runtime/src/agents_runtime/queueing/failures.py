"""Transitório e permanente — a decisão entre insistir e desistir.

Errar para o lado transitório queima cinco tentativas num payload que nunca vai
funcionar. Errar para o permanente joga fora uma mensagem que só precisava
esperar o 429 passar. Por isso a classificação é explícita e testada, e o que
não está mapeado é marcado como tal em vez de adivinhado.
"""

import re
from enum import Enum


class Failure(Enum):
    """O que fazer com o erro."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    # Repete como transitório, MAS o limite de tentativas continua valendo —
    # sem ele, "transitório" viraria "para sempre" e a DLQ, letra morta. Existir
    # separado é o que permite alertar quando a tabela abaixo envelhecer.
    UNKNOWN = "unknown"


_STATUS = re.compile(r"\b(?:HTTP\s*)?([1-5]\d{2})\b")

# O que o provedor respondeu vale mais que o tipo da exceção: um 400 embrulhado
# em RuntimeError é permanente, e um 500 no mesmo tipo é transitório.
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}

_TRANSIENT_TYPES = (TimeoutError, ConnectionError)
_PERMANENT_TYPES = (ValueError, PermissionError, LookupError)

_TRANSIENT_TEXT = ("deadlock", "timeout", "temporarily unavailable")
_PERMANENT_TEXT = ("credencial revogada", "payload inválido", "tenant inexistente")


def classify(error: BaseException) -> Failure:
    status = _STATUS.search(str(error))
    if status is not None:
        code = int(status.group(1))
        return Failure.TRANSIENT if code in _TRANSIENT_STATUS else Failure.PERMANENT

    if isinstance(error, _TRANSIENT_TYPES):
        return Failure.TRANSIENT
    if isinstance(error, _PERMANENT_TYPES):
        return Failure.PERMANENT

    message = str(error).lower()
    if any(term in message for term in _TRANSIENT_TEXT):
        return Failure.TRANSIENT
    if any(term in message for term in _PERMANENT_TEXT):
        return Failure.PERMANENT

    return Failure.UNKNOWN
