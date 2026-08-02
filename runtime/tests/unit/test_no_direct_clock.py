"""Fitness function — the clock is injected, never read.

`agents_runtime.clock` is the adapter layer for time. Anywhere else, calling
`datetime.now()`, `time.time()` or `asyncio.sleep()` reintroduces the real wall
clock into a rule that a test needs to move by hand — and the cost is not
visible until E3, when a cooldown test starts taking 72 hours.

Detection is by AST, not by grep: a comment mentioning `datetime.now()` is not
a violation, and `from time import sleep; sleep(30)` is.
"""

import ast
from pathlib import Path

import pytest

import agents_runtime

_SOURCE_ROOT = Path(agents_runtime.__file__).parent

# The one file allowed to read the real clock — it is what everything else
# injects. See agents_runtime/clock.py.
_ADAPTER_LAYER = {Path("clock.py")}

# Dotted calls, matched on the suffix so `datetime.datetime.now()` counts too.
_FORBIDDEN_CALLS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("datetime", "today"),
    ("date", "today"),
    ("time", "time"),
    ("time", "monotonic"),
    ("time", "perf_counter"),
    ("time", "sleep"),
    ("asyncio", "sleep"),
    ("loop", "time"),
}

# `from time import sleep` — the module is gone by the time the call is made.
_FORBIDDEN_IMPORTS = {
    "time": {"time", "monotonic", "perf_counter", "sleep"},
    "asyncio": {"sleep"},
}

# `import time as t` / `from datetime import datetime as dt` — the canonical
# name is gone from the call site; the alias has to be traced back to it.
_ALIASABLE_MODULES = {"time", "asyncio", "datetime"}
_ALIASABLE_CLASSES = {"datetime", "date"}


def _dotted_suffix(node: ast.expr) -> tuple[str, str] | None:
    """('datetime', 'now') for `datetime.now`, `dt.datetime.now`, `x.time`."""
    if not isinstance(node, ast.Attribute):
        return None
    owner = node.value
    if isinstance(owner, ast.Name):
        return (owner.id, node.attr)
    if isinstance(owner, ast.Attribute):
        return (owner.attr, node.attr)
    return None


def _violations(source: str) -> list[str]:
    tree = ast.parse(source)

    aliased: dict[str, str] = {}
    renamed: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _ALIASABLE_MODULES and alias.asname:
                    renamed[alias.asname] = alias.name
        if isinstance(node, ast.ImportFrom) and node.module in _FORBIDDEN_IMPORTS:
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORTS[node.module]:
                    aliased[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                if alias.name in _ALIASABLE_CLASSES and alias.asname:
                    renamed[alias.asname] = alias.name

    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in aliased:
            found.append(f"line {node.lineno}: {aliased[node.func.id]}()")
            continue
        suffix = _dotted_suffix(node.func)
        if suffix is not None:
            suffix = (renamed.get(suffix[0], suffix[0]), suffix[1])
        if suffix in _FORBIDDEN_CALLS:
            found.append(f"line {node.lineno}: {suffix[0]}.{suffix[1]}()")
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
def test_module_does_not_read_the_clock_directly(module: Path) -> None:
    found = _violations(module.read_text(encoding="utf-8"))

    assert not found, (
        f"{module.relative_to(_SOURCE_ROOT)} reads the clock directly "
        f"({'; '.join(found)}). Inject agents_runtime.clock.Clock instead."
    )


class TestTheDetectorItself:
    """A fitness function nobody has seen fail is a decoration."""

    def test_catches_a_dotted_call(self) -> None:
        assert _violations("import datetime\ndatetime.datetime.now()\n")

    def test_catches_an_imported_name(self) -> None:
        assert _violations("from time import sleep\nsleep(30)\n")

    def test_catches_an_aliased_import(self) -> None:
        assert _violations("from asyncio import sleep as nap\nnap(1)\n")

    def test_catches_an_aliased_module(self) -> None:
        assert _violations("import time as t\nt.time()\n")

    def test_catches_an_aliased_class(self) -> None:
        assert _violations("from datetime import datetime as dt\ndt.now()\n")

    def test_ignores_a_mention_in_a_comment(self) -> None:
        assert not _violations("# never call datetime.now() here\nx = 1\n")

    def test_ignores_an_injected_clock(self) -> None:
        assert not _violations("async def f(clock):\n    await clock.sleep(30)\n")
