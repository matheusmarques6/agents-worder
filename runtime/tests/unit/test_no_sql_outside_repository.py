"""Fitness function — SQL lives in the repository layer, nowhere else.

Import-linter already forbids domain modules from importing the driver, but a
module can hold a query string and hand it to someone else to run. The rule the
architecture actually states is about the SQL, not about the import: a query
written next to a business rule escapes review, escapes RLS reasoning and
escapes the `SET LOCAL app.tenant_id` discipline that goes with every statement.

Docstrings are exempt — prose quoting a query is not a query.
"""

import ast
import re
from pathlib import Path

import pytest

import agents_runtime

_SOURCE_ROOT = Path(agents_runtime.__file__).parent

# The only package allowed to contain SQL (arquitetura §3).
_REPOSITORY_LAYER = "repository"

_SQL_START = re.compile(
    r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE|ALTER|DROP|TRUNCATE"
    r"|GRANT|REVOKE|WITH|COPY|SET\s+LOCAL)\b",
    re.IGNORECASE,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the Constant nodes that are docstrings, so prose is not a finding."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        if _SQL_START.match(node.value):
            found.append(f"line {node.lineno}: {node.value.strip()[:60]!r}")
    return found


def _modules_outside_the_repository() -> list[Path]:
    return sorted(
        path
        for path in _SOURCE_ROOT.rglob("*.py")
        if path.relative_to(_SOURCE_ROOT).parts[0] != _REPOSITORY_LAYER
    )


@pytest.mark.parametrize(
    "module",
    _modules_outside_the_repository(),
    ids=lambda p: p.relative_to(_SOURCE_ROOT).as_posix(),
)
def test_module_contains_no_sql(module: Path) -> None:
    found = _violations(module.read_text(encoding="utf-8"))

    assert not found, (
        f"{module.relative_to(_SOURCE_ROOT)} contains SQL "
        f"({'; '.join(found)}). Move the query into agents_runtime/repository/."
    )


class TestTheDetectorItself:
    def test_catches_a_select(self) -> None:
        assert _violations('q = "SELECT id FROM conversations"\n')

    def test_catches_a_multiline_statement(self) -> None:
        assert _violations('q = """\n    UPDATE conversations SET lease_until = %s\n"""\n')

    def test_catches_a_cte(self) -> None:
        assert _violations('q = "WITH claimed AS (SELECT 1) SELECT * FROM claimed"\n')

    def test_ignores_a_docstring_quoting_sql(self) -> None:
        assert not _violations('"""SELECT max(seq)+1 is forbidden — see ADR-04."""\n')

    def test_ignores_ordinary_prose(self) -> None:
        assert not _violations('label = "Selecione uma loja"\n')
