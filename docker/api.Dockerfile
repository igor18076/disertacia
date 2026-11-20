FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS base
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM dependencies AS test-dependencies
RUN uv sync --locked --no-dev --group test --no-editable

FROM base AS application
COPY alembic.ini ./
COPY migrations ./migrations
USER 10001:10001

FROM application AS runtime
COPY --from=dependencies /app/.venv /app/.venv
CMD ["uvicorn", "coursegen.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM application AS tests
COPY --from=test-dependencies /app/.venv /app/.venv
COPY pyproject.toml ./
COPY tests ./tests
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "tests"]
