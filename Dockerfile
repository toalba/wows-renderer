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

COPY pyproject.toml ./
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -e "."

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

# Re-install the project itself (editable install needs the source)
COPY pyproject.toml ./
RUN pip install --no-deps -e .

ENTRYPOINT ["python", "-m", "bot.main"]
