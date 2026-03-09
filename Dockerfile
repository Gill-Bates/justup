# justUp! - Lightweight Uptime Monitor

# ──────────────────────────────────────────────────────────────
# Stage 1: Dependencies
# IMPORTANT: Use -bookworm tag to match runtime glibc (2.36)
# NOTE: For fully reproducible builds, pin to digest:
#       FROM python:3.13-slim-bookworm@sha256:<digest>
# ──────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS deps-builder

WORKDIR /build

# Fail fast: ensure GeoLite2 City DB exists on build host
COPY data/GeoLite2-City.mmdb /dist/GeoLite2-City.mmdb

# Build dependencies for native libs (matplotlib, cartopy, reportlab) and C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gcc \
    g++ \
    libgeos-dev \
    libffi-dev \
    libfreetype6-dev \
    libjpeg-dev \
    libproj-dev \
    proj-bin \
    proj-data \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# Python deps (shared by both build modes)
COPY requirements.txt .

# PIP_NO_BINARY was removed: cartopy/shapely wheels are available for amd64+arm64
# If cross-compilation issues occur, re-enable with: ENV PIP_NO_BINARY=cartopy,shapely
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip \
 && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt \
 && pip install --no-cache-dir --root-user-action=ignore cython setuptools wheel


# ──────────────────────────────────────────────────────────────
# Stage 1b: Clean Python deps for runtime copy (strip build-only tools)
# ──────────────────────────────────────────────────────────────
FROM deps-builder AS clean-deps

# Direct rm is safer than 'pip uninstall pip' which can leave partial state
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/Cython* \
           /usr/local/lib/python3.13/site-packages/cython* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/lib/python3.13/site-packages/_distutils_hack* \
           /usr/local/lib/python3.13/site-packages/distutils-precedence.pth \
 && find /usr/local/lib/python3.13/site-packages -type d -name "tests" -prune -exec rm -rf {} + || true


# ──────────────────────────────────────────────────────────────
# Stage 2: Cartopy Data
# ──────────────────────────────────────────────────────────────
FROM deps-builder AS cartopy-data

RUN mkdir -p /dist/cartopy_data && \
    CARTOPY_DATA_DIR=/dist/cartopy_data python -c "\
import cartopy.io.shapereader as shpreader; \
shpreader.natural_earth(resolution='110m', category='physical', name='land'); \
shpreader.natural_earth(resolution='110m', category='physical', name='ocean'); \
shpreader.natural_earth(resolution='110m', category='cultural', name='admin_0_countries'); \
shpreader.natural_earth(resolution='110m', category='physical', name='coastline'); \
print('Cartopy data downloaded successfully')"

# ──────────────────────────────────────────────────────────────
# Stage 2b: Flag Icons Data
# ──────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS flags-data

ARG FLAG_ICONS_VERSION=main

RUN apt-get update && apt-get install -y --no-install-recommends \
     ca-certificates \
     curl \
     tar \
 && curl -fsSL --retry 8 --retry-all-errors --retry-delay 5 \
     "https://github.com/lipis/flag-icons/archive/refs/heads/${FLAG_ICONS_VERSION}.tar.gz" \
     -o /tmp/flag-icons.tar.gz \
 && mkdir -p /dist/flags \
 && tar -xzf /tmp/flag-icons.tar.gz -C /tmp \
 && cp /tmp/flag-icons-${FLAG_ICONS_VERSION}/flags/4x3/*.svg /dist/flags/ \
 && rm -rf /var/lib/apt/lists/* /tmp/flag-icons.tar.gz /tmp/flag-icons-${FLAG_ICONS_VERSION}

# ──────────────────────────────────────────────────────────────
# Stage 3: Cython Build
# ──────────────────────────────────────────────────────────────
FROM deps-builder AS cython-builder

ARG TARGETARCH

# App source (will not appear in final runtime layer directly)
COPY app/ app/
COPY VERSION VERSION
COPY BUILD_INFO BUILD_INFO
COPY docker/setup_cython.py setup_cython.py

# Build Cython extensions in-place (cython/setuptools already installed in Stage 1)
RUN if [ "$TARGETARCH" = "arm64" ]; then export CFLAGS="-O2 -fno-strict-aliasing"; fi \
 && python setup_cython.py build_ext --inplace \
 #
 # ── Cleanup: Remove source files for compiled modules (prevent source code leak) ──
 && echo "=== Removing source files for compiled modules ===" \
 #
 # For each .so file anywhere in app/, remove the matching .py/.pyx/.pxd source (keep __init__.py)
 && find app/ -name "*.so" | while read -r so_file; do \
        base="${so_file%.cpython*}"; \
        dir="$(dirname "$so_file")"; \
        stem="$(basename "$base")"; \
        if [ "$stem" != "__init__" ]; then \
            rm -fv "${dir}/${stem}.py" "${dir}/${stem}.pyx" "${dir}/${stem}.pxd" || true; \
        fi; \
    done \
 #
 # Remove all C/C++ build intermediates
 && find app/ -type f \( -name "*.c" -o -name "*.cpp" \) ! -name "__init__.*" -delete \
 #
 # Remove __pycache__ directories (bytecode of removed sources)
 && find app/ -type d -name "__pycache__" -prune -exec rm -rf {} + || true \
 #
 # Verification: Check that each compiled .so has no lingering source file
 && echo "=== Verification ===" \
 && echo "Compiled extensions:" \
 && find app/ -name "*.so" -print \
 && echo "" \
 && echo "Checking for source leaks (compiled modules with remaining source):" \
 && LEAK_FOUND=0 \
 && for so_file in $(find app/ -name "*.so"); do \
        base="${so_file%.cpython*}"; \
        dir="$(dirname "$so_file")"; \
        stem="$(basename "$base")"; \
        for ext in .py .pyx .pxd; do \
            if [ -f "${dir}/${stem}${ext}" ] && [ "$stem" != "__init__" ]; then \
                echo "SOURCE LEAK: ${dir}/${stem}${ext}"; \
                LEAK_FOUND=1; \
            fi; \
        done; \
    done \
 && if [ "$LEAK_FOUND" = "1" ]; then exit 1; fi \
 && echo "(none - OK)" \
 && echo "=== Cleanup complete ==="


# ──────────────────────────────────────────────────────────────
# Stage 4: Runtime
# ──────────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS runtime

# OCI Labels (set via --build-arg or defaults)
ARG APP_VERSION=dev
ARG BUILD_DATE
ARG VCS_REF
ARG TARGETPLATFORM

LABEL maintainer="Gill-Bates" \
      org.opencontainers.image.title="justUp!" \
      org.opencontainers.image.description="Lightweight Uptime Monitor" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.platform="${TARGETPLATFORM}" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/justUp" \
      org.opencontainers.image.licenses="AGPL-3.0"

# Runtime deps - single RUN to minimize layers
# 1. Install prerequisites for Adoptium repo setup
# 2. Add Adoptium GPG key with fingerprint verification
# 3. Install all packages including temurin-21-jre
# 4. Download and verify signal-cli
# 5. Security: Use capabilities instead of SUID for ping, remove gpg+curl after use
ARG SIGNAL_CLI_VERSION=0.13.23
ARG SIGNAL_CLI_SHA256=4f30a3353ca3eaf4e80266ab422776e1d7a38b92b3cb2acb7e11dbb41f917223

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gpg \
 && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public -o /tmp/adoptium.key \
 && gpg --import --import-options show-only /tmp/adoptium.key 2>&1 | grep -q "3B04D753C9050D9A5D343F39843C48A565F8F04B" \
 && gpg --dearmor -o /usr/share/keyrings/adoptium.gpg /tmp/adoptium.key \
 && rm /tmp/adoptium.key \
 && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" > /etc/apt/sources.list.d/adoptium.list \
 && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libgeos-c1v5 \
    libfreetype6 \
    libjpeg62-turbo \
    libproj25 \
    zlib1g \
    mtr-tiny \
    whatweb \
    gosu \
    iputils-ping \
    libcap2-bin \
    locales \
    temurin-21-jre \
 && curl -fsSL --retry 5 --retry-delay 10 \
     "https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz" \
     -o /tmp/signal-cli.tar.gz \
 && echo "${SIGNAL_CLI_SHA256}  /tmp/signal-cli.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/signal-cli.tar.gz -C /opt \
 && rm /tmp/signal-cli.tar.gz \
 && ln -s "/opt/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli" /usr/local/bin/signal-cli \
 && chmod +x /usr/local/bin/signal-cli \
 && printf '#!/bin/sh\nexec /usr/local/bin/signal-cli --config "${SIGNAL_DATA_DIR:-/app/data/signal}" "$@"\n' \
     > /usr/local/bin/signal \
 && chmod +x /usr/local/bin/signal \
 # Security: Grant raw socket capability for ping and mtr (required as non-root)
 && setcap cap_net_raw+ep /bin/ping \
 && setcap cap_net_raw+ep /usr/bin/mtr \
 && chmod u-s /bin/ping \
 # Remove build-only tools explicitly, then clean up orphaned dependencies
 && apt-get purge -y gpg curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
 && locale-gen

# Non-root user with dedicated home directory (security: avoid HOME=/tmp)
RUN groupadd -r justup && useradd -r -g justup justup \
 && mkdir -p /home/justup/.matplotlib \
 && chown -R justup:justup /home/justup

WORKDIR /app

# Python dependencies from clean-deps (build-only tooling removed)
COPY --from=clean-deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# App source incl. built Cython extensions → /src/app/ (clean separation from /app/ workdir)
COPY --from=cython-builder --chown=justup:justup /build/app/ /src/app/

# Copy country flag SVGs (4x3) for local static serving in traceroute table
# Source: https://github.com/lipis/flag-icons
RUN mkdir -p /src/app/static/vendor/images/flags && chown justup:justup /src/app/static/vendor/images/flags
COPY --from=flags-data --chown=justup:justup /dist/flags/ /src/app/static/vendor/images/flags/

# Copy app metadata files (for auditability / license compliance, not used at runtime)
COPY --chown=justup:justup VERSION BUILD_INFO requirements.txt LICENSE CHANGELOG.md /app/

# CLI tool already included in app/ from cython-builder - create symlink only
RUN chmod +x /src/app/justup-cli \
 && chown justup:justup /src/app/justup-cli \
 && ln -s /src/app/justup-cli /usr/local/bin/justup-cli

# Copy pre-downloaded Cartopy data from cartopy-data stage
COPY --from=cartopy-data --chown=justup:justup /dist/cartopy_data /app/.cartopy

# GeoIP DB for traceroute geolocation
# Staged to /opt/defaults/ - entrypoint seeds to $JUSTUP_DATA_DIR if missing
# (avoids file being hidden when user mounts a volume at /app/data/)
COPY --from=deps-builder /dist/GeoLite2-City.mmdb /opt/defaults/GeoLite2-City.mmdb

# ─── DATA DIRECTORIES ──────────────────────────────────────────
RUN mkdir -p \
      /app/data \
      /app/data/sqlite \
      /app/data/tsdb \
      /app/data/reports \
      /app/data/signal \
 && chown -R justup:justup /app/data /app/.cartopy

# Entrypoint (runs as root, uses exec to hand off to gosu)
COPY docker/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod 0755 /docker-entrypoint.sh

EXPOSE 8080

# Health-Check using Python (curl removed from runtime for security)
# NOTE: Spawns Python interpreter (~30 MB RSS) per check. Acceptable at 30s intervals.
# For lower overhead, use: CMD ["/src/app/justup-cli", "healthcheck"] if implemented.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; r=urllib.request.urlopen(f'http://localhost:{os.environ.get(\"JUSTUP_PORT\",8080)}/health',timeout=3); sys.exit(0 if r.status==200 else 1)"

# Environment variables
# Security: Use dedicated home directory instead of /tmp
# Flexibility: Split JAVA heap sizes for easier runtime customization
# Structure: /src/app/ = Python package, /app/ = workdir with data/config
ENV HOME=/home/justup \
    MPLCONFIGDIR=/home/justup/.matplotlib \
    CARTOPY_DATA_DIR=/app/.cartopy \
    XDG_DATA_HOME=/home/justup \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/src \
    PYTHONFAULTHANDLER=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    JUSTUP_HOST=0.0.0.0 \
    JUSTUP_PORT=8080 \
    JUSTUP_DATA_DIR=/app/data \
    SIGNAL_CLI_PATH=/usr/local/bin/signal-cli \
    SIGNAL_DATA_DIR=/app/data/signal \
    JAVA_XMS=64m \
    JAVA_XMX=256m

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
