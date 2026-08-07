"""Fitness function — uma coluna que ninguém escreve é um requisito que evaporou.

`connector_accounts.sync_status` e `connector_accounts.last_sync_at` nasceram no
E1 e passaram **dois marcos** sem uma única linha de código que as escrevesse. O
dicionário as descrevia, a migration as criava, o hub as mostraria vazias para
sempre, e nada em lugar nenhum reprovava. Elas só apareceram por acaso, no S8,
quando a reconciliação foi escrever ali e descobriu que o lugar já existia.

Esse é o modo de falha que esta trava existe para tornar barulhento, e ele é
específico do jeito como este produto é construído: o dicionário de dados vem
**antes** do código, então toda coluna existe por um tempo sem consumidor por
projeto. O que não pode acontecer é esse tempo ser indefinido e silencioso.

**A regra:** toda coluna de `public` e `internal` tem de ser nomeada em algum
lugar do produto que não seja a própria declaração dela. O corpus é o produto —
`runtime/src`, as Edge Functions, o hub, e as migrations com as declarações
removidas. Os **testes ficam de fora de propósito**: uma fábrica de teste que
preenche a coluna é exatamente o disfarce que fez `sync_status` parecer viva
durante dois marcos.

**As exceções são uma lista, não uma tolerância.** Cada coluna calada hoje está
em `MUTE_BY_DESIGN` com o marco que vai lhe dar escritor. A lista é verificada
nas DUAS direções, e é a segunda que impede que ela vire um cemitério: uma
entrada cuja coluna JÁ ganhou escritor reprova e tem de sair. Uma lista que só
cresce documenta a dívida; uma lista que também encolhe a cobra.

**O limite desta trava, dito em voz alta, porque uma trava que se vende por mais
do que é vale menos que nenhuma:** ela prova que alguém NOMEIA a coluna, não que
alguém a ESCREVE. Uma coluna lida e nunca escrita passa — e existe uma agora
mesmo: `channels_accounts.warmup_stage` é lido por `repository/engine.py` e
alimenta a escada de warm-up de `queueing/antiban.py`, e nada em lugar nenhum a
avança de 0. O aquecimento 20→50→100 da D10 está, na prática, preso no primeiro
degrau. Distinguir leitura de escrita exigiria interpretar SQL com expressão
regular, o que produziria uma trava que mente em vez de uma que avisa; então a
trava afirma o que consegue afirmar, e o resto está escrito aqui.
"""

import re
from pathlib import Path

import psycopg
import pytest

#: A raiz do repositório: runtime/tests/db/<este arquivo>.
_ROOT = Path(__file__).resolve().parents[3]

#: O produto. Note a ausência de `runtime/tests` e de `hub/e2e`: uma coluna que
#: só um teste escreve não tem escritor nenhum.
_PRODUCT_GLOBS = (
    "runtime/src/**/*.py",
    "supabase/functions/**/*.ts",
    "hub/app/**/*.ts",
    "hub/app/**/*.tsx",
    "hub/components/**/*.ts",
    "hub/components/**/*.tsx",
)

_MIGRATIONS = "supabase/migrations/*.sql"

_SCHEMAS = ("public", "internal")

#: Colunas sem ninguém, hoje, e o marco que lhes dá voz. A justificativa não é
#: decoração: sem ela a lista vira "as que já estavam aqui", que é o estado de
#: onde `sync_status` saiu.
MUTE_BY_DESIGN: dict[str, str] = {
    # --- E4: o formulário de onboarding e o gerador de prompt ---------------
    "public.agent_versions.author_user_id": "E4 — quem gerou a versão; hoje só o gerador de teste",
    "public.agent_versions.change_summary": "E4 — o resumo que o gerador escreve junto da versão",
    "public.agent_versions.parent_version_id": "E4/E5 — a linhagem de versões (rascunho → ativa)",
    "public.agent_versions.escalation_config": "E4 — camada do prompt, mapeada do formulário",
    "public.agent_versions.price_policy": "E4 — camada do prompt, mapeada do formulário",
    "public.agent_versions.product_presentation": "E4 — camada do prompt, mapeada do formulário",
    "public.agent_versions.scheduling_config": "E4 — camada do prompt, mapeada do formulário",
    "public.agent_versions.activated_at": "E4 — a ativação de uma versão é ação de tela",
    "public.tenants.active_version_id": "E4 — apontada quando o lojista ativa a versão",
    "public.channels_accounts.display_name": "E4 — o apelido do número, dado na conexão",
    "public.profiles.full_name": "E4/E5 — o perfil é preenchido no cadastro",
    "public.profiles.is_platform_admin": "E6 — a marca de admin da plataforma",
    "public.memberships.permissions": "E5 — permissões finas por membro",
    # --- E5/E6: hub e admin --------------------------------------------------
    "public.alerts.resolved_at": "E6 — resolver um alerta é ação de tela",
    "public.conversations.closed_at": "E5 — encerrar conversa é ação de tela (ou do TTL, E7)",
    "public.messages.author_user_id": "E5 — o takeover humano assina a mensagem que envia",
    "internal.eval_runs.started_at": "E6 — a bateria de calibração sob demanda",
    # --- E7/E8: endurecimento e conectores restantes -------------------------
    "public.tenants.cancelled_at": "E7 — a purga de lojista cancelado (10 dias)",
    "public.tenants.followup_enabled": "E3 followups / E5 — sem leitor e sem escritor",
    "public.connector_accounts.webhooks_registered": "E8 — o registro de webhook por plataforma",
    "public.customers.total_spent": "E8 — agregado do espelho, quando o conector o trouxer",
    "public.orders.platform_updated_at": "E8 — o carimbo de atualização da plataforma",
    "internal.message_outbox.payload_hash": "E7 — impressão do payload, auditoria de duplicidade",
    # --- diagnóstico do processo --------------------------------------------
    "internal.runtime_heartbeats.started_at": (
        "o instante em que o processo subiu; `beat` grava só a batida. "
        "Escritor previsto no E6, junto do painel de saúde do runtime"
    ),
    "public.channels_accounts.warmup_started_at": (
        "D10 — o warm-up do número Evolution. Ver o limite descrito no docstring: "
        "`warmup_stage` tem leitor e nenhum escritor, e este par é o mesmo buraco"
    ),
}


def _strip_declarations(sql: str) -> str:
    """A migration menos as declarações — o que sobra é quem USA a coluna.

    Sem isso a trava seria vacuamente verde: toda coluna é nomeada pelo `CREATE
    TABLE` que a criou, e foi exatamente essa a menção que fez `sync_status`
    parecer existir.
    """
    sql = re.sub(r"^\s*--.*$", "", sql, flags=re.MULTILINE)
    # `create table x ( ... \n);` — o fecho na coluna 0 é o estilo do repositório.
    sql = re.sub(r"create\s+table[^(]*\(.*?^\);", "", sql, flags=re.DOTALL | re.MULTILINE | re.I)
    sql = re.sub(r"alter\s+table\s+[\w.\"]+\s+add\s+column.*?;", "", sql, flags=re.DOTALL | re.I)
    sql = re.sub(r"comment\s+on\s+column.*?;", "", sql, flags=re.DOTALL | re.I)
    return sql


@pytest.fixture(scope="module")
def product_source() -> str:
    parts = [
        path.read_text(encoding="utf-8")
        for glob in _PRODUCT_GLOBS
        for path in _ROOT.glob(glob)
    ]
    parts += [
        _strip_declarations(path.read_text(encoding="utf-8"))
        for path in sorted(_ROOT.glob(_MIGRATIONS))
    ]
    assert parts, f"no product source found under {_ROOT} — the corpus globs are wrong"
    return "\n".join(parts)


@pytest.fixture(scope="module")
def columns(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            """
            select c.table_schema, c.table_name, c.column_name
              from information_schema.columns c
              join information_schema.tables t
                on t.table_schema = c.table_schema
               and t.table_name = c.table_name
             where c.table_schema = any(%s)
               and t.table_type = 'BASE TABLE'
             order by 1, 2, 3
            """,
            (list(_SCHEMAS),),
        ).fetchall()
    assert rows, "information_schema returned nothing — the schema is not applied"
    return [".".join(row) for row in rows]


def _mute(columns: list[str], source: str) -> set[str]:
    return {
        column
        for column in columns
        if not re.search(rf"\b{re.escape(column.split('.')[-1])}\b", source)
    }


def test_no_column_exists_that_the_product_never_names(
    columns: list[str], product_source: str
) -> None:
    unexplained = sorted(_mute(columns, product_source) - set(MUTE_BY_DESIGN))

    assert unexplained == [], (
        "colunas sem escritor e sem justificativa: "
        + ", ".join(unexplained)
        + ". Ou o código que as preenche está faltando, ou elas entram em "
        "MUTE_BY_DESIGN com o marco que vai escrevê-las."
    )


def test_the_exception_list_has_no_column_that_already_found_its_writer(
    columns: list[str], product_source: str
) -> None:
    """A direção que impede a lista de virar cemitério.

    Uma entrada obsoleta é pior que uma coluna calada: ela afirma, por escrito,
    que ninguém escreve algo que alguém passou a escrever — e é assim que a
    próxima pessoa a ler a lista deixa de acreditar nela.
    """
    stale = sorted(set(MUTE_BY_DESIGN) - _mute(columns, product_source))

    assert stale == [], (
        "estas colunas ganharam escritor e continuam listadas como caladas: "
        + ", ".join(stale)
        + ". Remova a entrada de MUTE_BY_DESIGN no mesmo PR."
    )


def test_the_exception_list_names_only_columns_that_exist(columns: list[str]) -> None:
    """E a terceira direção: uma coluna renomeada ou removida deixa para trás uma
    entrada que protege um nome que não existe mais — uma isenção fantasma que
    cobriria a próxima coluna a herdar o nome."""
    unknown = sorted(set(MUTE_BY_DESIGN) - set(columns))

    assert unknown == [], "MUTE_BY_DESIGN cita colunas que não existem: " + ", ".join(unknown)
