# Runtime image — a single asyncio process on the VPS (ADR-1).
#
# Two stages so the final image carries no build tooling. Dependencies are
# installed in their own layer, before the source is copied: editing a module
# then rebuilds in seconds instead of re-resolving the whole dependency tree.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first — this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# As rubricas do Judge 1 são ativos versionados, não código: o responder as lê
# na largada e sem elas o portão não tem contrato. Ficam fora do pacote porque
# são as MESMAS que o pack de evals usa — duas cópias divergiriam.
COPY evals/ ./evals/


FROM python:3.13-slim-bookworm AS runtime

# Never run as root: the container talks to Postgres and to external APIs, and
# a compromised process should not also own the filesystem.
RUN useradd --create-home --uid 10001 runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=builder --chown=runtime:runtime /app/.venv /app/.venv
COPY --from=builder --chown=runtime:runtime /app/src /app/src
COPY --from=builder --chown=runtime:runtime /app/evals /app/evals

USER runtime

# Graceful shutdown matters (core/testes-e-cicd.md §6.3): on SIGTERM the process
# stops claiming, finishes in-flight leases and VTs, and only then exits — the
# queue absorbs the gap. Exec form so the signal reaches Python, not a shell.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "agents_runtime"]
