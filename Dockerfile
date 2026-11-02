FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY wildpatch ./wildpatch
COPY web ./web
ENV PORT=8000
CMD ["sh", "-c", "uv run --frozen --no-dev uvicorn wildpatch.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
