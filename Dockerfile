# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, only the venv and app code.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 ap
WORKDIR /app
COPY --from=build --chown=ap:ap /app /app
# The service appends live actions to /app/data/audit.jsonl, so the folder must exist and belong to the app user.
RUN mkdir -p /app/data && chown ap:ap /app/data
USER ap
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8887 APMATCH_DATA_DIR=/app/data APMATCH_POS=25000
EXPOSE 8887
# Startup simulates a year of AP on 25,000 POs, 25 to 70 seconds depending on the machine.
HEALTHCHECK --interval=15s --timeout=3s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8887/healthz', timeout=2)"
CMD ["apmatch", "serve"]
