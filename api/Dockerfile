# syntax=docker/dockerfile:1

# --- dev: bind-mounted source, hot reload ---
FROM python:3.13-slim AS dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PATH="/root/.local/bin:${PATH}"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer first so source edits do not invalidate it.
COPY pyproject.toml ./
RUN uv sync --no-install-project

COPY . .

EXPOSE 8000
# The request-context middleware emits a richer access record (request id,
# duration, path), so uvicorn's own plain-text access log would duplicate it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--reload"]

# --- prod: no reload, no dev tooling ---
FROM python:3.13-slim AS prod

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PATH="/root/.local/bin:${PATH}"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
RUN uv sync --no-install-project --no-dev

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
