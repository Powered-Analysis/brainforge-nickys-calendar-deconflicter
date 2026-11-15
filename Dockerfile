FROM python:3.14-slim

# Same uv version CI uses, so a new uv release can't change the build on its own.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

# Dependencies first, so a code-only change reuses the cached layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY .env.example ./
RUN uv sync --locked --no-dev

EXPOSE 8080
CMD ["uvicorn", "dispatcher.app:app", "--host", "0.0.0.0", "--port", "8080"]
