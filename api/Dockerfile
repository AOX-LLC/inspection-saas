# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM python:3.12.15-slim-trixie AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp
WORKDIR /srv/api
COPY --from=uv /uv /usr/local/bin/uv

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM deps AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini pyproject.toml ./
COPY tests/ tests/
USER 10003:10003
CMD ["pytest"]

# Last stage, so it is the default build target. Built from the plain Python
# image, not from base, so the final image does not contain uv.
FROM python:3.12.15-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/tmp
WORKDIR /srv/api
RUN groupadd --gid 10001 api \
    && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init \
       --shell /usr/sbin/nologin api
COPY --from=deps /opt/venv /opt/venv
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
