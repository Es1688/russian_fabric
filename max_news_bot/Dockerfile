# Build context is the REPO ROOT (pyproject.toml and uv.lock live there and are
# copied before the code to cache the dependency layer):
#   docker build -f max_news_bot/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

# MAX Bot API (platform-api2.max.ru) serves TLS certificates issued by the
# Russian national CA ("Russian Trusted Root CA", NUC/Minchtsifry). The root is
# absent from Debian ca-certificates and certifi, so HTTPS to *.max.ru fails
# with SSLError unless trusted explicitly; see max_news_bot/certs/README.md.
COPY max_news_bot/certs/russian_trusted_root_ca.crt /usr/local/share/ca-certificates/russian_trusted_root_ca.crt
RUN update-ca-certificates

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY max_news_bot ./max_news_bot/

ENV PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "max_news_bot.main"]
