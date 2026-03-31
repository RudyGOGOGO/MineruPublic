# =================
#   Builder stage
# =================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Configure the Python directory so it is consistent
ENV UV_PYTHON_INSTALL_DIR=/python

# Only use the managed Python version
ENV UV_PYTHON_PREFERENCE=only-managed

# Install Python before the project for caching
RUN uv python install 3.12

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev
COPY mineru /app/mineru
COPY pyproject.toml pyrightconfig.json uv.lock \
    README.md CONTRIBUTING.md llm-config.defaults.jsonc LICENSE \
    /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# =================
#    Final stage
# =================
FROM debian:bookworm-slim

# Install required dependencies for ui-auto
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl adb && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Use non-root user
RUN useradd -m -s /bin/bash --create-home ui-auto && \
    mkdir -p /home/ui-auto/.android && \
    chown -R ui-auto:ui-auto /home/ui-auto/.android
USER ui-auto

WORKDIR /app

# Copy the Python version
COPY --from=builder --chown=python:python /python /python

# Copy the application from the builder
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"

COPY --chown=ui-auto:ui-auto docker-entrypoint.sh /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
