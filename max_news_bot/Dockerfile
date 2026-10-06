# Build context is the REPO ROOT (pyproject.toml and uv.lock live there and are
# copied before the code to cache the dependency layer):
#   docker build -f max_news_bot/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY max_news_bot ./max_news_bot/

ENV PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "max_news_bot.main"]
