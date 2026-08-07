"""Fixtures for the `load` level — the pipeline's, reused rather than rebuilt.

The load suite runs the SAME composition the pipeline suite runs, against the
same database, with the same clean slate. Rebuilding those fixtures here would
be rebuilding the thing under test: two definitions of "an empty world" is two
definitions that can disagree, and the one that disagrees silently is the one
that makes a load report mean nothing.

So this module re-exports them. What the load level adds is the burst and the
report, and nothing else.
"""

from tests.pipeline.conftest import (  # noqa: F401
    _testing_schema,
    admin,
    clean_slate,
    dsn,
    queue_length,
    sync_admin,
    tiny_config,
)

try:  # pragma: no cover — Windows only, same reason as the pipeline conftest
    from tests.pipeline.conftest import pytest_asyncio_loop_factories  # noqa: F401
except ImportError:  # pragma: no cover
    pass
