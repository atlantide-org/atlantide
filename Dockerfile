# syntax=docker/dockerfile:1.9
#
# Runtime image for the `atlantide` CLI.
#
#   docker build -t atlantide .
#   docker run --rm -v "$PWD:/work" -u "$(id -u):$(id -g)" atlantide plan
#
# The Postgres state backend is an extra, so it is opt-in here too:
#
#   docker build --build-arg EXTRAS="--extra postgres" -t atlantide .

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.9.16

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# Builder — resolve and install dependencies into a self-contained virtualenv.
#
# Shares a base image with the runtime stage deliberately. A virtualenv records
# the absolute path of the interpreter that created it, so one built on a
# different image is subtly broken once copied across.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-alpine AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

ARG EXTRAS=""

# Dependencies first, from the lockfile alone. This layer is keyed on
# pyproject.toml and uv.lock only, so editing the source neither re-resolves nor
# re-downloads anything — which is most of the build time.
#
# Runtime dependencies only. What keeps pytest, hypothesis, moto, mypy and ruff
# out is that they live in the `dev` extra and no `--extra dev` is passed: uv
# installs the default dependencies and nothing else. `--no-dev` is belt and
# braces for the day those move to a [dependency-groups] table, where it would
# be the flag that excludes them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-dev --no-install-project ${EXTRAS}

# Then the project, which is the only layer a source edit invalidates.
# --no-editable so the virtualenv holds a real copy and nothing points at /app.
# README.md is required: pyproject declares it as the project readme, and the
# wheel build fails without it.
COPY atlantide/ /app/atlantide/
COPY pyproject.toml uv.lock README.md /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${EXTRAS}

# ---------------------------------------------------------------------------
# Runtime — the virtualenv, a git binary, and nothing else.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-alpine AS runtime

RUN apk add --no-cache git

RUN adduser -D -u 10001 -s /sbin/nologin atlantide

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work

USER atlantide

ENTRYPOINT ["atlantide"]
CMD ["--help"]

# The build workflow overwrites these with the real revision and source URL.
LABEL org.opencontainers.image.title="atlantide" \
      org.opencontainers.image.description="Typed, deterministic Infrastructure-as-Code for Python." \
      org.opencontainers.image.source="https://github.com/atlantide-org/atlantide" \
      org.opencontainers.image.licenses="GPL-3.0-only"
