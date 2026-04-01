FROM python:3.12-slim AS builder

# System deps for pycairo build + git for SSH deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    pkg-config \
    libcairo2-dev \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies (SSH forwarded for private repos)
COPY pyproject.toml ./
RUN --mount=type=ssh \
    mkdir -p ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts && \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -e "."

# --- Runtime stage ---
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    ffmpeg \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application code + gamedata
COPY renderer/ renderer/
COPY bot/ bot/
COPY wows-gamedata/ wows-gamedata/

# Install WoWS fonts for correct text rendering
RUN mkdir -p /usr/local/share/fonts/wows && \
    cp wows-gamedata/data/gui/fonts/*.ttf /usr/local/share/fonts/wows/ && \
    fc-cache -f

# Re-install the project itself (editable install needs the source)
COPY pyproject.toml ./
RUN pip install --no-deps -e .

ENTRYPOINT ["python", "-m", "bot.main"]
