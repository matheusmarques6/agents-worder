"""E3 · S7 — o ritmo que um número tem direito de falar (D10).

Anti-ban é **do sender**: jitter, warm-up e teto diário são ritmo de entrega por
número, e o sender é o único que fala com a API (ADR-8). Conteúdo não passa por
aqui — a variação de copy foi decidida antes da outbox, porque
`message_outbox.payload` é o conteúdo final.

Duas assimetrias que este arquivo existe para fixar, e as duas são de produto,
não de engenharia:

1. **A reativa nunca espera e nunca estoura teto.** O `CLAUDE.md` é explícito:
   resposta reativa não é limitada por taxa, o único anti-flood dela é o
   debounce. Um jitter de 30-120s numa resposta a cliente é o cliente esperando
   dois minutos por um "oi" — é o erro que mata atendimento, e ele tem sabotagem
   dedicada.
2. **A Cloud não passa por este módulo.** Lá o teto é o tier do Meta, e quem o
   aplica é a escada (rung `channel_paused_tier`), antes da outbox. Anti-ban é
   da Evolution, onde o risco é o número ser banido e não a conta ser suspensa.

Motivos são afirmados como string literal, e não importando o vocabulário do
módulo: a lição do S4 do E2 — um teste que cita a constante que verifica inverte
junto com ela e não prova nada.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agents_runtime.queueing.antiban import Pacing, allowance, daily_allowance
from tests.support.clock import FrozenClock

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class FixedRandomness:
    """O acaso, congelado numa fração conhecida da faixa."""

    def __init__(self, fraction: float = 0.5) -> None:
        self.fraction = fraction
        self.asked: list[tuple[float, float]] = []

    def uniform(self, low: float, high: float) -> float:
        self.asked.append((low, high))
        return low + (high - low) * self.fraction


def _pacing(**overrides) -> Pacing:
    """Um envio proativo por Evolution que nada objeta."""
    facts: dict[str, object] = {
        "channel_type": "evolution",
        "proactive": True,
        "risk_accepted": True,
        # Estágio 3 = warm-up concluído, o teto passa a ser o duro.
        "warmup_stage": 3,
    }
    facts.update(overrides)
    return Pacing(**facts)  # type: ignore[arg-type]


class TestTheHappyPath:
    def test_a_proactive_nothing_objects_to_goes_out(self) -> None:
        verdict = allowance(_pacing(), FrozenClock(NOW), FixedRandomness())

        assert verdict.allow is True
        assert verdict.reason == "allowed"


class TestTheJitterIsPerNumber:
    def test_the_next_send_is_pushed_between_thirty_and_a_hundred_and_twenty(self) -> None:
        # `CLAUDE.md`: jitter 30-120s. Um número que dispara em cadência de
        # relógio é um número que se denuncia sozinho.
        randomness = FixedRandomness(fraction=0.5)

        verdict = allowance(_pacing(), FrozenClock(NOW), randomness)

        assert randomness.asked == [(30.0, 120.0)]
        assert verdict.wait == timedelta(seconds=75)

    def test_the_number_waits_out_its_own_jitter(self) -> None:
        # A trava é do NÚMERO, não da mensagem: o toque seguinte só existe
        # depois que o intervalo sorteado passou.
        verdict = allowance(
            _pacing(next_send_at=NOW + timedelta(seconds=40)),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.allow is False
        assert verdict.reason == "jitter_wait"
        # Quanto falta, para o sender devolver a linha à fila pelo tempo certo
        # em vez de por um chute.
        assert verdict.wait == timedelta(seconds=40)

    def test_a_jitter_already_expired_does_not_hold_anything(self) -> None:
        verdict = allowance(
            _pacing(next_send_at=NOW - timedelta(seconds=1)),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.allow is True

    def test_the_randomness_is_injected_and_not_drawn_here(self) -> None:
        # Mesma razão do relógio: uma regra que sorteia sozinha não é
        # reprodutível, e `test_no_direct_randomness.py` reprova o build se um
        # segundo módulo tocar `random`.
        low = allowance(_pacing(), FrozenClock(NOW), FixedRandomness(0.0))
        high = allowance(_pacing(), FrozenClock(NOW), FixedRandomness(1.0))

        assert low.wait == timedelta(seconds=30)
        assert high.wait == timedelta(seconds=120)


class TestTheWarmUpLadder:
    @pytest.mark.parametrize(
        ("stage", "limit"),
        [(0, 20), (1, 50), (2, 100)],
    )
    def test_each_stage_has_its_own_ceiling(self, stage: int, limit: int) -> None:
        # 20 → 50 → 100, o número do `CLAUDE.md`. Um chip novo que dispara 300
        # no primeiro dia é um chip queimado no primeiro dia.
        assert daily_allowance(_pacing(warmup_stage=stage)) == limit

    def test_a_finished_warm_up_hands_over_to_the_hard_cap(self) -> None:
        assert daily_allowance(_pacing(warmup_stage=3, daily_cap=300)) == 300

    def test_the_warm_up_ceiling_blocks_with_its_own_name(self) -> None:
        # O motivo distingue "este número ainda está aquecendo" de "este número
        # bateu o teto do dia" — dois diagnósticos e duas ações diferentes.
        verdict = allowance(
            _pacing(warmup_stage=0, sends_today=20), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is False
        assert verdict.reason == "warmup_cap"

    def test_a_daily_cap_tighter_than_the_warm_up_wins(self) -> None:
        # O lojista pode apertar; o warm-up é o teto da plataforma. Nunca o
        # contrário — é a mesma direção única do teto de proativos da D1.
        assert daily_allowance(_pacing(warmup_stage=2, daily_cap=40)) == 40


class TestTheHardDailyCap:
    def test_the_last_send_under_the_cap_still_goes(self) -> None:
        verdict = allowance(
            _pacing(sends_today=299, daily_cap=300), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is True

    def test_the_send_that_would_break_the_cap_does_not(self) -> None:
        verdict = allowance(
            _pacing(sends_today=300, daily_cap=300), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is False
        assert verdict.reason == "daily_cap"

    def test_the_cap_is_the_accounts_and_not_a_constant(self) -> None:
        # `channels_accounts.daily_cap` existe desde o S2 e ninguém a lia.
        # Guarda sem alvo mente.
        verdict = allowance(
            _pacing(sends_today=50, daily_cap=50), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is False
        assert verdict.reason == "daily_cap"


class TestTheRiskAcceptance:
    def test_nothing_proactive_leaves_before_the_merchant_accepts_the_risk(self) -> None:
        # A Evolution é canal não oficial: quem assume o risco de banimento é o
        # lojista, por escrito (`risk_accepted_at` + `audit_log`). Antes disso o
        # primeiro envio não existe.
        verdict = allowance(
            _pacing(risk_accepted=False), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is False
        assert verdict.reason == "risk_not_accepted"

    def test_the_risk_outranks_every_other_refusal(self) -> None:
        # Precedência afirmada, como na escada: um número sem aceite reportado
        # como "esperando o jitter" faria o operador esperar por algo que nunca
        # vai acontecer sozinho.
        verdict = allowance(
            _pacing(
                risk_accepted=False,
                sends_today=999,
                next_send_at=NOW + timedelta(seconds=60),
            ),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.reason == "risk_not_accepted"


class TestWhatThisModuleNeverTouches:
    def test_a_reactive_reply_never_waits_for_a_jitter(self) -> None:
        # A metade que mata atendimento se quebrar.
        verdict = allowance(
            _pacing(proactive=False, next_send_at=NOW + timedelta(minutes=5)),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.allow is True
        assert verdict.wait is None

    def test_a_reactive_reply_never_hits_the_daily_cap(self) -> None:
        verdict = allowance(
            _pacing(proactive=False, sends_today=100_000, warmup_stage=0),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.allow is True

    def test_a_reactive_reply_goes_out_even_without_the_risk_acceptance(self) -> None:
        # Um contato que escreveu está esperando resposta. Recusar a resposta
        # dele por causa de uma pendência administrativa entre nós e o lojista
        # seria punir a pessoa errada — e o aceite protege o lojista de um
        # banimento por DISPARO, que é o que ele não pediu.
        verdict = allowance(
            _pacing(proactive=False, risk_accepted=False), FrozenClock(NOW), FixedRandomness()
        )

        assert verdict.allow is True

    def test_the_cloud_channel_does_not_pass_through_here_at_all(self) -> None:
        # Lá o teto é o tier do Meta e quem o aplica é a escada, antes da
        # outbox. Duas regras para dois riscos diferentes.
        verdict = allowance(
            _pacing(channel_type="cloud", risk_accepted=False, sends_today=10_000),
            FrozenClock(NOW),
            FixedRandomness(),
        )

        assert verdict.allow is True
        assert verdict.wait is None
