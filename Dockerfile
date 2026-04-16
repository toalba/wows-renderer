FROM python:3.12-slim AS builder

# System deps for pycairo build + git for fetching the parser over HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    pkg-config \
    libcairo2-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached when only source changes).
# `uv sync --no-install-project` resolves + installs everything from
# pyproject.toml/uv.lock except the project itself.
COPY pyproject.toml uv.lock LICENSE README.md ./
RUN uv sync --no-install-project --no-dev

# Copy source and finalise the install.
COPY renderer/ renderer/
COPY bot/ bot/
RUN uv sync --no-dev

# --- Runtime stage ---
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    git \
    ffmpeg \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code.
# NOTE: wows-gamedata is NOT baked into the image — it is volume-mounted at
# runtime (see docker-compose.yml). This keeps the image small and lets a
# single image serve many game versions via the per-version gamedata cache.
# WoWS fonts live inside the gamedata mount; if correct font rendering is
# required, install them on the host or via an entrypoint hook that runs
# `fc-cache -f` against the mounted directory.
COPY renderer/ renderer/
COPY bot/ bot/
COPY scripts/ scripts/
COPY render_quick.py ./
# pyproject.toml is referenced by the editable install's .pth file in
# .venv/ (created by uv sync in the builder stage). It must exist at
# runtime for module discovery to work.
COPY pyproject.toml ./

# Liveness: the bot touches /tmp/bot_heartbeat every 30s from its event loop.
# A stale file (>120s) means the loop is stuck or the task died.
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os,time,sys; sys.exit(0 if os.path.exists('/tmp/bot_heartbeat') and time.time()-os.path.getmtime('/tmp/bot_heartbeat')<120 else 1)"

ENTRYPOINT ["python", "-m", "bot.main"]
