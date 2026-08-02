"""Fitness function — o acaso é injetado, nunca sorteado no lugar.

Mesma doença do relógio, mesmo remédio. Jitter, escolha de variação de copy e
qualquer sorteio de política precisam ser reprodutíveis num teste; um
`random.uniform()` no meio da regra transforma o nível `unit` em loteria — ele
passa oito vezes e reprova na nona, e ninguém consegue dizer se foi o código ou
o dado.

`agents_runtime.randomness` é a única casa autorizada, e este teste falha se
aparecer uma segunda.

Não é uma proibição de todo acaso: `uuid4()` para um token de lease continua
livre, porque nenhum teste precisa prever o valor de um token. O alvo é o
módulo `random`, que é onde mora o sorteio que uma regra observa.
"""

import ast
from pathlib import Path

import pytest

import agents_runtime

_SOURCE_ROOT = Path(agents_runtime.__file__).parent
_ADAPTER_LAYER = {Path("randomness.py")}

_FORBIDDEN_MODULE = "random"


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)

    aliases: set[str] = set()
    imported_names: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _FORBIDDEN_MODULE:
                    aliases.add(alias.asname or alias.name)
        if isinstance(node, ast.ImportFrom) and node.module == _FORBIDDEN_MODULE:
            for alias in node.names:
                imported_names[alias.asname or alias.name] = alias.name

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in imported_names:
            found.append(f"linha {node.lineno}: random.{imported_names[node.func.id]}()")
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in aliases
        ):
            found.append(f"linha {node.lineno}: {node.func.value.id}.{node.func.attr}()")
    return found


def _runtime_modules() -> list[Path]:
    return sorted(
        path
        for path in _SOURCE_ROOT.rglob("*.py")
        if path.relative_to(_SOURCE_ROOT) not in _ADAPTER_LAYER
    )


@pytest.mark.parametrize(
    "module", _runtime_modules(), ids=lambda p: p.relative_to(_SOURCE_ROOT).as_posix()
)
def test_module_does_not_draw_its_own_randomness(module: Path) -> None:
    found = _violations(module.read_text(encoding="utf-8"))

    assert not found, (
        f"{module.relative_to(_SOURCE_ROOT)} sorteia direto "
        f"({'; '.join(found)}). Injete agents_runtime.randomness.Randomness."
    )


class TestTheDetectorItself:
    def test_catches_a_dotted_call(self) -> None:
        assert _violations("import random\nrandom.uniform(0, 1)\n")

    def test_catches_an_aliased_module(self) -> None:
        assert _violations("import random as r\nr.random()\n")

    def test_catches_an_imported_name(self) -> None:
        assert _violations("from random import uniform\nuniform(0, 1)\n")

    def test_ignores_an_injected_source(self) -> None:
        assert not _violations("def f(randomness):\n    return randomness.uniform(0, 1)\n")

    def test_ignores_uuid(self) -> None:
        assert not _violations("import uuid\ntoken = uuid.uuid4()\n")
