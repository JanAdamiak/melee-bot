FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Install dependencies before copying source for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY *.py ./
COPY melee_tiebreakers/ ./melee_tiebreakers/
COPY domain/ ./domain/
COPY commands/ ./commands/

# Mount /data as a volume for SQLite persistence across restarts
VOLUME ["/data"]
ENV DB_PATH=/data/melee.db
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "bot.py"]
