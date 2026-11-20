# One image for both processes. The default command is the API; the worker runs the same
# image with: python -m app.worker.main. Migrations: python -m app.db
FROM python:3.12-slim

# uv installs the exact versions pinned in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv

WORKDIR /app
# No cache mount is used, so don't keep uv's download cache in the image.
ENV UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1

# Dependencies first, in their own layer, so code-only changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
