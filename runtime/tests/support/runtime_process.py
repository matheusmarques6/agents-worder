"""The kill harness — the real process, as a real subprocess.

Started with `sys.executable -m agents_runtime` against the test database,
with tiny intervals injected through the environment (`config_from_env`).
Waiting is always BY PREDICATE AGAINST THE DATABASE — never log parsing,
never a blind sleep: the database is the one observable the process and the
test share, and it is the same observable production monitoring will use.

`subprocess.Popen`, not asyncio's subprocess, on purpose: the pipeline suite
runs under a selector event loop (psycopg needs it on Windows), and selector
loops cannot manage child processes on Windows. Popen works under any loop,
and `kill()` is TerminateProcess there — which is exactly the unclean death
cenário C wants.
"""

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[2]

TINY_INTERVALS = {
    "AGENTS_COALESCER_TICK_MS": "50",
    "AGENTS_IDLE_PAUSE_MS": "50",
    "AGENTS_SENDER_POLL_MS": "50",
    "AGENTS_PROCESS_HEARTBEAT_MS": "100",
    "AGENTS_BUSY_RETRY_MS": "100",
}


class RuntimeProcess:
    def __init__(self, dsn: str, *, name: str, extra_env: dict[str, str] | None = None):
        self.name = name
        self._dsn = dsn
        self._extra_env = extra_env or {}
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        env = {
            **os.environ,
            "SUPABASE_DB_URL": self._dsn,
            "AGENTS_PROCESS_NAME": self.name,
            "AGENTS_WORKER_SET_ROLE": "worker_role",
            "AGENTS_SENDER_SET_ROLE": "sender_role",
            "AGENTS_CHANNEL": "tests.support.fake_channel:create_channel",
            # Declared next to the channel, for the same reason: both seams are
            # the harness's choice, not a default the process invents. A test
            # that wants another responder overrides it through `extra_env`.
            "AGENTS_RESPONDER": "tests.support.constant_reply:create_responder",
            "PYTHONPATH": str(RUNTIME_ROOT),
            **TINY_INTERVALS,
            **self._extra_env,
        }
        self._process = subprocess.Popen(
            [sys.executable, "-m", "agents_runtime"],
            cwd=RUNTIME_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def kill(self) -> None:
        """The unclean death: SIGKILL / TerminateProcess. No goodbyes."""
        assert self._process is not None
        self._process.kill()
        self._process.wait(timeout=10)

    def terminate(self) -> int:
        """The clean death — POSIX only, where terminate() is SIGTERM."""
        assert self._process is not None
        self._process.terminate()
        return self._process.wait(timeout=15)

    def still_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def output(self) -> str:
        assert self._process is not None and self._process.stdout is not None
        return self._process.stdout.read().decode(errors="replace")

    def __enter__(self) -> "RuntimeProcess":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.still_running():
            self._process.kill()
            self._process.wait(timeout=10)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 15.0, note: str = "") -> None:
    """Poll a database predicate until true — the harness's only kind of waiting.

    The poll interval is real time, which is fine at this level: `pipeline`
    owns real I/O by definition. What stays forbidden is waiting for a fixed
    duration and hoping.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(f"predicate never became true within {timeout}s: {note}")
