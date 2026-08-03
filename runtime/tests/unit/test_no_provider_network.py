"""Fitness function — the network has exactly two doors, and no test opens one.

Blocking suites (PR and merge) never touch the external network; only the
weekly `contract` suite does (`core/testes-e-cicd.md` §2). The rule is easy to
state and easy to break by accident: someone debugging an adapter pastes a real
URL into a `unit` test, it passes on their machine, and from then on the gate
depends on a third party being up — and on nobody noticing that the "unit"
suite is billing tokens.

So the address of a provider is a privileged literal:

  * inside `agents_runtime`, only the two ADAPTERS may name a provider host —
    `channels/cloud_api.py` (Meta) and `agent_core/openrouter.py` (the ONE LLM
    route, D1). Everything else receives a port;
  * inside `tests`, only `tests/contract/` may name one.

Detection is by AST like the clock and randomness locks, and for the same
reason: prose is not a violation. A docstring explaining which host is allowed
must not be the thing that fails the build (decisão 48) — so docstrings are
skipped, and only real string literals count.

This file exempts itself, exactly as the clock lock exempts `clock.py`: the
detector cannot be written without spelling out what it hunts for.
"""

import ast
from pathlib import Path

import pytest

import agents_runtime

_SOURCE_ROOT = Path(agents_runtime.__file__).parent
_TESTS_ROOT = Path(__file__).parents[1]

# The two doors. Relative to the package root.
_ADAPTERS = {
    Path("channels/cloud_api.py"),
    Path("agent_core/openrouter.py"),
}

# Relative to tests/. The contract level is the one allowed to reach a provider,
# plus this detector, which has to name what it forbids.
_ALLOWED_TESTS = {Path("unit/test_no_provider_network.py")}
_CONTRACT_LEVEL = "contract"

_SCHEMES = ("http://", "https://")

# A local address is not a provider: the OTLP receiver of the Alloy sidecar
# (S10) and any test server live here.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the Constant nodes that are docstrings — prose, not addresses."""
    prose: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                prose.add(id(first.value))
    return prose


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)
    prose = _docstring_nodes(tree)

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in prose:
            continue
        text = node.value
        for scheme in _SCHEMES:
            start = text.find(scheme)
            if start < 0:
                continue
            host = text[start + len(scheme) :].split("/")[0]
            if host.split(":")[0] in _LOCAL_HOSTS or host.startswith(_LOCAL_HOSTS):
                continue
            found.append(f"line {node.lineno}: {scheme}{host}")
    return found


def _runtime_modules() -> list[Path]:
    return sorted(
        path
        for path in _SOURCE_ROOT.rglob("*.py")
        if path.relative_to(_SOURCE_ROOT) not in _ADAPTERS
    )


def _test_modules() -> list[Path]:
    return sorted(
        path
        for path in _TESTS_ROOT.rglob("*.py")
        if path.relative_to(_TESTS_ROOT).parts[0] != _CONTRACT_LEVEL
        and path.relative_to(_TESTS_ROOT) not in _ALLOWED_TESTS
    )


@pytest.mark.parametrize(
    "module", _runtime_modules(), ids=lambda p: p.relative_to(_SOURCE_ROOT).as_posix()
)
def test_only_an_adapter_names_a_provider(module: Path) -> None:
    found = _violations(module.read_text(encoding="utf-8"))

    assert not found, (
        f"{module.relative_to(_SOURCE_ROOT)} names an external host "
        f"({'; '.join(found)}). Only {sorted(str(p) for p in _ADAPTERS)} may; "
        "everything else receives a port."
    )


@pytest.mark.parametrize(
    "module", _test_modules(), ids=lambda p: p.relative_to(_TESTS_ROOT).as_posix()
)
def test_no_blocking_test_reaches_a_provider(module: Path) -> None:
    found = _violations(module.read_text(encoding="utf-8"))

    assert not found, (
        f"tests/{module.relative_to(_TESTS_ROOT).as_posix()} names an external host "
        f"({'; '.join(found)}). Blocking suites never touch the network — move it "
        "to tests/contract/, which is not part of any gate."
    )


class TestTheDetectorItself:
    """A fitness function nobody has seen fail is a decoration."""

    def test_catches_a_provider_url(self) -> None:
        assert _violations('BASE = "https://openrouter.ai/api/v1"\n')

    def test_catches_a_host_hidden_in_a_longer_string(self) -> None:
        assert _violations('url = f"call https://graph.facebook.com/v19.0 now"\n')

    def test_ignores_a_mention_in_a_docstring(self) -> None:
        assert not _violations('"""We talk to https://openrouter.ai from the adapter."""\n')

    def test_ignores_a_database_dsn(self) -> None:
        assert not _violations('dsn = "postgresql://postgres@db:5432/postgres"\n')

    def test_ignores_a_local_address(self) -> None:
        assert not _violations('otlp = "http://localhost:4318/v1/traces"\n')
