"""Fitness function — `max(seq)` nunca gera uma sequência.

O `CLAUDE.md` lista "No `SELECT max(seq)+1`" entre os invariantes
inegociáveis, e até aqui o invariante não tinha guarda nenhuma: existia como
frase. Descoberto ao preparar a sabotagem da A2, que assumia esta trava.

Por que a frase não basta: entre o SELECT e o INSERT cabe outra conexão
fazendo o mesmo. As duas leem o mesmo máximo, as duas gravam o mesmo número, e
o `UNIQUE (conversation_id, direction, seq)` reprova a segunda — em produção,
no meio de uma rajada, com o cliente esperando. O caminho oficial é
`internal.next_message_seq()`, um `UPDATE ... RETURNING` atômico por
construção.

A varredura cobre o SQL das migrations e o Python do runtime, porque a
tentação existe nos dois: em SQL é uma linha, em Python é um `execute()` com a
mesma linha dentro.
"""

import re
from pathlib import Path

import pytest

import agents_runtime

_RUNTIME = Path(agents_runtime.__file__).parent
# runtime/tests/unit/ -> runtime/tests -> runtime -> raiz do repositório
_MIGRATIONS = Path(__file__).parents[3] / "supabase" / "migrations"

# `max(seq)`, `max( seq )`, `MAX(m.seq)` — o alvo é a agregação sobre a coluna,
# em qualquer espaçamento e com ou sem alias de tabela.
_MAX_SEQ = re.compile(r"\bmax\s*\(\s*[\w.]*\bseq\b\s*\)", re.IGNORECASE)


def _without_comments(source: str) -> str:
    """Prosa não é violação — a mesma lição do detector de cor do hub.

    Um comentário explicando por que `max(seq)+1` é proibido é documentação; se
    a trava o flagra, ela ensina a parar de documentar.
    """
    without_blocks = re.sub(r"/\*[\s\S]*?\*/", " ", source)
    without_sql_line = re.sub(r"--.*$", "", without_blocks, flags=re.MULTILINE)
    without_py_line = re.sub(r"(^|[^:])#.*$", r"\1", without_sql_line, flags=re.MULTILINE)
    # Docstrings de Python e blocos de corpo de função SQL ($$...$$) contêm
    # SQL de verdade, então NÃO são removidos: só as três formas de comentário.
    return without_py_line


def _scanned_files() -> list[Path]:
    return sorted(_RUNTIME.rglob("*.py")) + sorted(_MIGRATIONS.glob("*.sql"))


def _violations(source: str) -> list[str]:
    return [
        f"linha {index + 1}: {line.strip()[:70]!r}"
        for index, line in enumerate(_without_comments(source).splitlines())
        if _MAX_SEQ.search(line)
    ]


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: p.name)
def test_sequences_do_not_come_from_an_aggregate(path: Path) -> None:
    found = _violations(path.read_text(encoding="utf-8"))

    assert not found, (
        f"{path.name} gera sequência com max(seq) ({'; '.join(found)}). "
        "Use internal.next_message_seq() — UPDATE ... RETURNING é atômico; "
        "SELECT max(seq)+1 entrega o mesmo número a duas conexões."
    )


class TestTheDetectorItself:
    """Trava que ninguém viu falhar é decoração."""

    def test_catches_the_canonical_form(self) -> None:
        assert _violations("select max(seq) + 1 from messages\n")

    def test_catches_it_with_an_alias(self) -> None:
        assert _violations("select coalesce(MAX(m.seq), 0) + 1 from messages m\n")

    def test_catches_it_inside_python(self) -> None:
        assert _violations('cur.execute("select max(seq)+1 from public.messages")\n')

    def test_ignores_a_sql_comment(self) -> None:
        assert not _violations("-- max(seq)+1 é proibido, ver ADR-6b\nselect 1\n")

    def test_ignores_a_python_comment(self) -> None:
        assert not _violations("# nunca use max(seq)+1 aqui\nx = 1\n")

    def test_ignores_max_over_another_column(self) -> None:
        assert not _violations("select max(created_at) from public.messages\n")
