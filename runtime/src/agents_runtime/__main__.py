"""Process entrypoint — what `python -m agents_runtime` (the image's CMD) runs.

The actual runtime loop — workers, coalescer, scheduler, senders as tasks of
one asyncio process — lands with E0-08. Until then, starting the process is a
deliberate, explanatory refusal instead of a ModuleNotFoundError at the first
container start.
"""


def main() -> None:
    raise SystemExit("agents-runtime: the runtime loop arrives with E0-08; nothing to run yet.")


if __name__ == "__main__":
    main()
