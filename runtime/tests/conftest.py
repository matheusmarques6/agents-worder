"""Shared pytest configuration.

The test level comes from the directory, not from the author remembering a
decorator: everything under `tests/db/` is `-m db`, everything under
`tests/pipeline/` is `-m pipeline`, and so on. A mislabelled test would run in
the wrong CI gate — the cheapest way to make that impossible is to stop asking
humans to label it.

`rls` is deliberately NOT derived from a directory: the RLS suite lives inside
`tests/db/` (core/testes-e-cicd.md §6.4) and is marked explicitly, so a DB test
and a leak test can sit side by side.
"""

from pathlib import Path

import pytest

_LEVEL_BY_DIRECTORY = {
    "unit": "unit",
    "db": "db",
    "pipeline": "pipeline",
    "contract": "contract",
}

_TESTS_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        try:
            relative = Path(item.path).relative_to(_TESTS_ROOT)
        except ValueError:  # pragma: no cover — a test outside tests/
            continue
        level = _LEVEL_BY_DIRECTORY.get(relative.parts[0])
        if level is not None:
            item.add_marker(getattr(pytest.mark, level))
