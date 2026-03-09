# =============================================================================
# JustUp Telemetry Collector - Dockerfile
# Multi-stage build for minimal image size
# =============================================================================

# --- Build stage: install dependencies ---
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Flag download stage ---
FROM debian:bookworm-slim AS flags

ARG FLAG_ICONS_VERSION=main

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/flags.tar.gz "https://github.com/lipis/flag-icons/archive/refs/heads/${FLAG_ICONS_VERSION}.tar.gz" \
 && mkdir -p /tmp/flags \
 && tar xzf /tmp/flags.tar.gz -C /tmp/flags --strip-components=1 \
 && mkdir -p /tmp/flag-output \
 && cp /tmp/flags/flags/4x3/*.svg /tmp/flag-output/ \
 && rm -rf /tmp/flags /tmp/flags.tar.gz

# --- Runtime stage ---
FROM python:3.13-slim

# Build arguments
ARG APP_VERSION=dev
ARG BUILD_DATE
ARG VCS_REF
ARG PUID=1000
ARG PGID=1000

LABEL maintainer="Gill-Bates" \
      description="JustUp Telemetry Collector Server" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.title="JustUp Tracking" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/justup"

# Create non-root user with no login shell (security hardening)
RUN groupadd -g ${PGID} tracking && \
    useradd -u ${PUID} -g tracking -s /sbin/nologin -M tracking

# Set working directory
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code (owned by tracking user)
COPY --chown=tracking:tracking main.py .
COPY --chown=tracking:tracking templates/ templates/
COPY --chown=tracking:tracking static/ static/

# Copy flag SVGs from flag stage (after static/ COPY, so it doesn't get overwritten)
RUN mkdir -p /app/static/flags
COPY --from=flags /tmp/flag-output/*.svg /app/static/flags/
RUN chown -R tracking:tracking /app/static/flags

# Create data directory
RUN mkdir -p /app/data && chown tracking:tracking /app/data

# Validate required templates exist (before switching user)
RUN test -f templates/dashboard.html \
 && test -f templates/instance.html \
 && test -f templates/login.html \
 || (echo "ERROR: Required templates missing" && exit 1)

# Create wrapper script for exec-form CMD (allows ENV expansion + proper signal handling)
RUN printf '#!/bin/sh\nexec uvicorn main:app --host ${HOST} --port ${PORT} --workers ${WORKERS}\n' \
    > /app/start.sh && chmod +x /app/start.sh

# Switch to non-root user
USER tracking

# Environment variables (defaults for production)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    APP_VERSION=${APP_VERSION} \
    TRACKING_DB_PATH=/app/data/tracking.db \
    GEOIP_DB_PATH=/app/data/GeoLite2-City.mmdb

# WARNING: WORKERS > 1 requires database-backed sessions (not yet implemented).
# Setting WORKERS > 1 will cause authentication issues on the admin dashboard.
# Current in-memory session store is NOT shared between workers.
ENV WORKERS=1

# Health check using Python (no curl dependency)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8080\")}/health')"]

# Expose port (documentation only)
EXPOSE 8080

# Run application using wrapper script (exec-form for proper signal handling)
CMD ["/app/start.sh"]
