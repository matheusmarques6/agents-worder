"""A fábrica do responder — o que o processo constrói quando sobe de verdade.

`AGENTS_RESPONDER` é uma fábrica `module:callable` que o `__main__` resolve com
o DSN (doutrina do `AGENTS_CHANNEL`, decisão 57): ausente e quebrada são estados
diferentes, e nenhum dos dois pode virar silenciosamente "o agente antigo".

E as rubricas do Judge 1 são **arquivo**, não código: se o caminho delas não
resolver, o processo tem de morrer alto na largada em vez de descobrir isso na
primeira mensagem de um cliente.
"""

from pathlib import Path

import pytest

import agents_runtime
from agents_runtime.agent_core.responder import (
    RUBRICS_DIRECTORY_VARIABLE,
    agent_responder,
    default_rubrics_directory,
)
from agents_runtime.evals.pack import load_rubrics


class TestTheRubrics:
    def test_the_default_directory_is_the_one_that_ships(self) -> None:
        """Derivado do pacote instalado, nunca do diretório de trabalho: o
        processo sobe dentro de um container cujo cwd não é o repositório."""
        directory = default_rubrics_directory()

        assert directory.is_dir(), f"{directory} não existe — o Judge 1 não teria contrato"
        assert directory == Path(agents_runtime.__file__).parents[2] / "evals" / "rubrics"

    def test_the_four_rubrics_of_the_milestone_load(self) -> None:
        rubrics = load_rubrics(default_rubrics_directory())

        assert set(rubrics) == {"factual", "tom_e_idioma", "seguranca", "escopo"}

    def test_the_environment_may_point_somewhere_else(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(RUBRICS_DIRECTORY_VARIABLE, str(tmp_path))

        assert default_rubrics_directory() == tmp_path


class TestTheFactory:
    def test_it_dies_at_startup_without_the_provider_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem chave não há modelo para onde rotear. Morrer aqui é a diferença
        entre um deploy que falha e um agente que emudece em produção."""
        monkeypatch.delenv("AGENTS_OPENROUTER_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="AGENTS_OPENROUTER_API_KEY"):
            agent_responder("postgresql://x")

    def test_it_builds_a_responder_when_the_key_is_there(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nada de rede: construir a porta não fala com ninguém — a primeira
        # chamada é que falaria, e ela não acontece aqui.
        monkeypatch.setenv("AGENTS_OPENROUTER_API_KEY", "sk-or-de-teste")

        assert callable(agent_responder("postgresql://x"))


class TestTheImage:
    def test_the_image_carries_the_rubrics(self) -> None:
        """Trava de implantação: a imagem copiava só `src/`, e o responder lê as
        rubricas de `evals/`. Sem esta linha o container subiria e morreria na
        primeira mensagem — em produção, não no CI."""
        dockerfile = (Path(agents_runtime.__file__).parents[2] / "Dockerfile").read_text(
            encoding="utf-8"
        )

        assert "evals" in dockerfile, (
            "o Dockerfile não copia `evals/` — o Judge 1 ficaria sem rubricas na imagem"
        )
