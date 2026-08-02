"""Fitness function — the image's CMD target exists.

The Dockerfile ends in `python -m agents_runtime`. Without a `__main__` in the
package, the image builds green and the container dies on its first breath
with ModuleNotFoundError — a failure that no test level would see before the
VPS does.
"""

import importlib.util


def test_the_package_has_a_main_module() -> None:
    assert importlib.util.find_spec("agents_runtime.__main__") is not None, (
        "the Dockerfile runs `python -m agents_runtime`, but the package has "
        "no __main__ module — the container would build and then die on start"
    )
