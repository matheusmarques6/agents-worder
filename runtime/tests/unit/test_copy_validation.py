"""E3 · S7 — o validador determinístico que ocupa a cadeira do Judge 1 (D3).

O Bruno decidiu em 2026-08-06 que **disparo e campanha não passam pelo Judge 1**;
só a resposta em tempo real passa. Isso deixa exatamente um ponto no produto em
que texto escrito por um modelo chega a um contato sem portão de LLM na frente —
a variação anti-ban de um toque de funil — e este módulo é o que ocupa a cadeira
vazia.

Por isso a regra é estreita e determinística: a variação só pode **variar** o
`copy_base` aprovado por um humano. Não pode introduzir **número**, **prazo**,
**link** nem **promessa** que a base não tenha. Violação → o toque não sai.

Cada teste aqui é uma regra, e cada regra existe porque o caso que ela barra é um
caso que chegaria ao telefone de uma pessoa: um preço que ninguém cotou, um prazo
que ninguém prometeu, um link que ninguém controla, um desconto que ninguém
aprovou.

O que este arquivo NÃO afirma, e não pode: que a variação é boa, verdadeira ou
gentil. O validador confere **forma**, nunca intenção — é o risco residual que a
D3 registra e que o Bruno aceitou por escrito.
"""

import pytest

from agents_runtime.dispatch.variation import (
    REGENERATION_LIMIT,
    VIOLATIONS,
    CopyRejected,
    validate,
    vary,
)

BASE = "Vi que ficou algo no carrinho. Quer que eu te ajude a finalizar?"


def scripted(*replies: str):
    """Um gerador de variações que devolve o roteiro, na ordem.

    O dublê injetável do E2, de novo: não há chave de LLM nesta máquina e não
    deveria haver — uma regra que só fecha contra a rede é uma regra que ninguém
    roda antes do merge.
    """
    pending = list(replies)
    calls: list[str] = []

    async def generate(base: str) -> str:
        calls.append(base)
        return pending.pop(0) if pending else replies[-1]

    generate.calls = calls  # type: ignore[attr-defined]
    return generate


class TestAVariationMayOnlyVary:
    def test_a_rewrite_that_says_the_same_thing_passes(self) -> None:
        # O caso positivo, e ele é o motivo do módulo existir: a variação anti-ban
        # tem que ser POSSÍVEL, ou o teto de 300/dia da Evolution é o único
        # remédio contra o banimento e a copy repetida entrega o número sozinha.
        variant = "Notei que você deixou uns itens no carrinho. Posso te ajudar a concluir?"

        assert validate(BASE, variant) == ()

    def test_the_base_itself_is_always_acceptable(self) -> None:
        # Ela foi aprovada por um humano. Se o próprio texto aprovado reprovasse,
        # o validador estaria medindo outra coisa que não "o que foi introduzido".
        assert validate(BASE, BASE) == ()

    def test_an_empty_variation_is_not_a_safe_one(self) -> None:
        # Silêncio parece inofensivo e não é: seria uma mensagem em branco no
        # telefone de alguém, com o nome da loja em cima.
        assert validate(BASE, "   ") == ("empty",)


class TestTheFourThingsAVariationMayNotInvent:
    def test_a_number_the_base_never_quoted_is_refused(self) -> None:
        # O caso que mais dói: um preço que a loja não cotou.
        variant = "Vi que ficou algo no carrinho. Fecho por R$ 49,90 pra você?"

        assert "introduced_number" in validate(BASE, variant)

    def test_a_number_the_base_did_quote_travels_freely(self) -> None:
        base = "Seu pedido 1234 está esperando. Quer finalizar?"
        variant = "O pedido 1234 continua reservado — quer concluir?"

        assert validate(base, variant) == ()

    def test_a_number_spelled_out_is_still_a_number(self) -> None:
        # O buraco que só um leitor de português enxerga: barrar "24h" e deixar
        # passar "vinte e quatro horas" é barrar a grafia, não o compromisso.
        variant = "Vi que ficou algo no carrinho. Guardo por vinte e quatro horas."

        assert "introduced_number" in validate(BASE, variant)

    def test_a_deadline_the_base_never_promised_is_refused(self) -> None:
        variant = "Vi que ficou algo no carrinho. As últimas unidades acabam hoje!"

        assert "introduced_deadline" in validate(BASE, variant)

    def test_a_deadline_the_base_already_carries_is_allowed(self) -> None:
        base = "Ainda dá tempo de finalizar hoje."
        variant = "Hoje ainda dá tempo — quer que eu finalize?"

        assert validate(base, variant) == ()

    def test_a_link_the_base_never_carried_is_refused(self) -> None:
        # Um domínio que a base não menciona é o caminho mais curto entre a nossa
        # mensagem e uma mensagem de phishing: quem recebe não tem como separar.
        #
        # Os endereços deste arquivo são **conteúdo de mensagem**, nunca destinos
        # — nada aqui disca. Ainda assim eles obedecem à trava de rede
        # (`test_no_provider_network`): host externo nenhum é escrito num teste
        # bloqueante, nem como texto. A trava é uma regra absoluta de propósito, e
        # contorná-la montando o esquema por pedaços seria exatamente a evasão que
        # ela existe para tornar visível. Então a forma com esquema é exercitada
        # contra o host local que a própria trava isenta, e a forma sem esquema —
        # a mais difícil para a regex, e a que um modelo escreveria — contra
        # domínios que não são de ninguém.
        variant = "Vi que ficou algo no carrinho. Finalize em https://localhost/promo"

        assert "introduced_link" in validate(BASE, variant)

    def test_a_bare_domain_counts_as_a_link(self) -> None:
        variant = "Vi que ficou algo no carrinho. Acesse oferta-imperdivel.xyz e conclua."

        assert "introduced_link" in validate(BASE, variant)

    def test_the_stores_own_link_travels_when_the_base_has_it(self) -> None:
        base = "Seu carrinho continua salvo: loja-exemplo.com.br/carrinho"
        variant = "Guardei seu carrinho aqui: loja-exemplo.com.br/carrinho"

        assert validate(base, variant) == ()

    def test_a_promise_the_base_never_made_is_refused(self) -> None:
        # A margem do lojista não é do modelo. "Frete grátis" para aquecer o tom
        # é uma decisão comercial tomada por quem não paga a conta.
        variant = "Vi que ficou algo no carrinho. O frete fica grátis pra você!"

        assert "introduced_promise" in validate(BASE, variant)

    def test_an_accent_does_not_dodge_the_gate(self) -> None:
        # Um portão que "grátis" atravessa mas "gratis" não é decoração.
        variant = "Vi que ficou algo no carrinho. Hoje sai gratis."

        assert "introduced_promise" in validate(BASE, variant)


class TestTheCopyNeverRepeatsTheLastOne:
    def test_repeating_the_previous_touch_verbatim_is_refused(self) -> None:
        # `CLAUDE.md`, anti-ban da Evolution: "copy never repeats the last one".
        # Copy repetida é a assinatura que faz o número ser denunciado.
        previous = "Notei que você deixou uns itens no carrinho."

        assert "repeats_previous" in validate(BASE, previous, previous=previous)

    def test_punctuation_is_not_a_variation(self) -> None:
        # Trocar o ponto por reticências não engana o provedor, e não pode
        # enganar o validador.
        previous = "Notei que você deixou uns itens no carrinho."
        variant = "notei que voce deixou uns itens no carrinho..."

        assert "repeats_previous" in validate(BASE, variant, previous=previous)

    def test_a_genuine_variation_passes_with_a_previous(self) -> None:
        previous = "Notei que você deixou uns itens no carrinho."
        variant = "Passei pra lembrar do seu carrinho — quer que eu finalize?"

        assert validate(BASE, variant, previous=previous) == ()


class TestTheVerdictIsData:
    def test_every_reason_reported_belongs_to_the_declared_vocabulary(self) -> None:
        # Mesma doutrina de `ladder.DENIAL_REASONS`: um motivo inventado no call
        # site é um balde de métrica que ninguém consegue agrupar.
        variant = "Fecho por R$ 10 hoje, com frete grátis: oferta-imperdivel.xyz/y"

        assert set(validate(BASE, variant)) <= set(VIOLATIONS)

    def test_all_four_families_are_reported_at_once(self) -> None:
        # O operador que vai ler isso precisa saber TUDO que estava errado. Uma
        # violação por vez faria três rodadas de investigação para um só toque.
        variant = "Fecho por R$ 10 hoje, com frete grátis: oferta-imperdivel.xyz/y"

        assert validate(BASE, variant) == (
            "introduced_number",
            "introduced_deadline",
            "introduced_link",
            "introduced_promise",
        )

    def test_the_verdict_is_pure(self) -> None:
        # Sem relógio, sem acaso, sem I/O: as mesmas três strings entram, a mesma
        # tupla sai, sempre. É o que faz disto um portão que um teste cobra, e
        # não uma heurística que deriva.
        variant = "Fecho por R$ 10 hoje!"

        assert validate(BASE, variant) == validate(BASE, variant)


class TestTheRegenerationBudget:
    async def test_a_clean_variation_is_used_as_it_is(self) -> None:
        generate = scripted("Passei pra lembrar do seu carrinho.")

        assert await vary(BASE, generate=generate) == "Passei pra lembrar do seu carrinho."
        assert len(generate.calls) == 1

    async def test_a_rejected_variation_is_asked_for_again(self) -> None:
        generate = scripted(
            "Fecho por R$ 49,90!",
            "Passei pra lembrar do seu carrinho.",
        )

        assert await vary(BASE, generate=generate) == "Passei pra lembrar do seu carrinho."
        assert len(generate.calls) == 2

    async def test_the_budget_is_finite_and_the_touch_does_not_go_out(self) -> None:
        # Teto 2, o mesmo que o Judge 1 tem pré-envio (`CLAUDE.md`), e pelo mesmo
        # motivo: um gerador que produziu lixo duas vezes não está a uma chamada
        # de produzir algo usável, e cada tentativa a mais é dinheiro e atraso
        # numa mensagem que já está atrasada.
        generate = scripted("Fecho por R$ 49,90!", "Só hoje por R$ 39,90!", "R$ 29,90!")

        with pytest.raises(CopyRejected) as raised:
            await vary(BASE, generate=generate)

        assert len(generate.calls) == REGENERATION_LIMIT
        assert "introduced_number" in raised.value.violations
        # O texto reprovado viaja no erro: é o que a linha de `alerts` vai
        # mostrar para o humano que precisa decidir se o gerador quebrou.
        assert raised.value.variant == "Só hoje por R$ 39,90!"

    async def test_the_previous_copy_reaches_the_gate_through_vary(self) -> None:
        # A regra "nunca repete a última" tem que valer no caminho real, não só
        # quando alguém chama `validate` à mão.
        previous = "Passei pra lembrar do seu carrinho."
        generate = scripted(previous, "Seu carrinho continua salvo por aqui.")

        assert await vary(BASE, generate=generate, previous=previous) == (
            "Seu carrinho continua salvo por aqui."
        )
