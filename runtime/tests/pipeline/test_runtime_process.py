"""The real process as a subprocess — the harness carries its own test.

A kill harness that cannot see the process live, or cannot kill it, would
decorate cenários C with false confidence. So before any scenario uses it,
the harness proves: the process starts and beats, a hard kill ends it, and —
where the platform can express it — a SIGTERM ends it CLEANLY, exit code 0,
which is the E0-08 graceful-shutdown property now covering every loop.
"""

import sys

import psycopg
import pytest

from tests.support.runtime_process import RuntimeProcess, wait_until


def beats(dsn: str, name: str):
    def check() -> bool:
        with psycopg.connect(dsn, autocommit=True) as conn:
            row = conn.execute(
                "select 1 from internal.runtime_heartbeats where process_name = %s",
                (name,),
            ).fetchone()
            return row is not None

    return check


def test_the_process_starts_and_beats_and_a_hard_kill_ends_it(dsn: str) -> None:
    with RuntimeProcess(dsn, name="harness-smoke") as process:
        wait_until(beats(dsn, "harness-smoke"), note="first heartbeat")
        assert process.still_running()

        process.kill()

        assert not process.still_running()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="terminate() is TerminateProcess on Windows — no clean signal; CI (Linux) runs this",
)
def test_sigterm_ends_the_process_cleanly(dsn: str) -> None:
    with RuntimeProcess(dsn, name="harness-graceful") as process:
        wait_until(beats(dsn, "harness-graceful"), note="first heartbeat")

        exit_code = process.terminate()

        assert exit_code == 0, "graceful shutdown is exit 0: claims stopped, in-flight finished"
