# syntax=docker/dockerfile:1
# justUp! - Lightweight Uptime Monitor
#
# Python dependencies are installed wheel-only from the hash-checked
# docker/requirements.lock (derived from pyproject.toml) plus the vendored SSLyze
# wheel in docker/vendor/wheels/; the release workflow passes PYTHON_BASE as a
# digest so both architectures share one base.
ARG PYTHON_BASE=python:3.13-slim
# Java LTS line of the signal-cli runtime; the newest patch of it is taken at build time.
ARG JAVA_MAJOR=25
# Rust toolchain of the libsignal build stage (both architectures). It must provide exactly the release in
# libsignal's rust-toolchain file (1.98.1 for libsignal 0.103.0, checked inside the stage);
# pinned by digest, since the stage compiles code that ends up in the image.
ARG RUST_IMAGE=rust:1.98.1-slim-trixie@sha256:4cd829461bd5c4d511c32e269da9cb8929223b666519d8004e35fc8d1d771ab7

# ──────────────────────────────────────────────────────────────
# Stage 1: Dependencies
# ──────────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS deps-builder

WORKDIR /build

# Python deps: pyproject.toml declares the direct dependencies, docker/requirements.lock
# pins the complete tested set with hashes (--require-hashes also rejects unpinned
# transitive packages). Installed as a standalone layer to keep dependency caching intact.
#
# Wheel-only on purpose: --only-binary=:all: means nothing is compiled and no build
# backend (setuptools/wheel/isolated build env) is ever downloaded or run, so the
# stage needs no compiler or -dev packages. sslyze is not taken from PyPI or a source
# archive but from the vendored, SHA-256-pinned justUp wheel (docker/vendor/wheels/,
# provenance in docker/vendor/PROVENANCE.json); --find-links points pip at it and the
# lock hash decides. A missing wheel, a wrong hash or a wrong platform fails the build.
# pip is the one shipped in the digest-pinned base image and is not upgraded here.
COPY docker/requirements.lock /tmp/requirements.lock
COPY docker/vendor/wheels/ /tmp/wheels/

RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes --only-binary=:all: \
        --find-links /tmp/wheels -r /tmp/requirements.lock \
 && pip check \
 && rm -rf /tmp/requirements.lock /tmp/wheels


# ──────────────────────────────────────────────────────────────
# Stage 1b: Clean Python deps for runtime copy (strip build-only tools)
# ──────────────────────────────────────────────────────────────
FROM deps-builder AS clean-deps

# Direct rm is safer than 'pip uninstall pip' which can leave partial state
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/lib/python3.13/site-packages/_distutils_hack* \
           /usr/local/lib/python3.13/site-packages/distutils-precedence.pth \
 && find /usr/local/lib/python3.13/site-packages -type d -name "tests" -prune -exec rm -rf {} + || true


# ──────────────────────────────────────────────────────────────
# Stage 2: App source
# ──────────────────────────────────────────────────────────────
FROM deps-builder AS app-source
# App source (pure Python, no compilation step)
COPY app/ app/
COPY pyproject.toml ./
# Remove __pycache__ directories
RUN find app/ -type d -name "__pycache__" -prune -exec rm -rf {} + || true
# ──────────────────────────────────────────────────────────────
# Stage 2b: signal-cli release (download verified against the published SHA-256)
# ──────────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS signal-cli-src

# latest release unless SIGNAL_CLI_VERSION is given (the release workflow
# resolves it once, so both architectures install the same build).
ARG SIGNAL_CLI_VERSION=""

RUN <<'EOF'
set -eu
python3 - <<'PY'
import hashlib, json, os, shutil, tarfile, urllib.request

version = os.environ.get("SIGNAL_CLI_VERSION", "").strip().removeprefix("v")
api = "https://api.github.com/repos/AsamK/signal-cli/releases/"
with urllib.request.urlopen(api + (f"tags/v{version}" if version else "latest"), timeout=60) as resp:
    release = json.load(resp)
version = release["tag_name"].removeprefix("v")
name = f"signal-cli-{version}.tar.gz"
asset = next(a for a in release["assets"] if a["name"] == name)
# Integrity check against the SHA-256 digest GitHub publishes for the asset
algo, _, expected = (asset.get("digest") or "").partition(":")
if algo != "sha256" or not expected:
    raise SystemExit(f"No sha256 digest published for {name}")
archive = "/tmp/signal-cli.tar.gz"
with urllib.request.urlopen(asset["browser_download_url"], timeout=300) as resp, open(archive, "wb") as fh:
    shutil.copyfileobj(resp, fh)
with open(archive, "rb") as fh:
    if hashlib.file_digest(fh, "sha256").hexdigest() != expected:
        raise SystemExit(f"Checksum mismatch for {name}")
with tarfile.open(archive) as tar:
    tar.extractall("/opt", filter="data")
os.rename(f"/opt/signal-cli-{version}", "/opt/signal-cli")
os.remove(archive)
print(f"Verified and unpacked signal-cli {version}")
PY
EOF

# ──────────────────────────────────────────────────────────────
# Stage 2b-i: libsignal for the target architecture, built from the official Signal sources
# ──────────────────────────────────────────────────────────────
# The signal-cli release bundles libsignal-client with a prebuilt native library for linux/amd64
# only and none for arm64. To get the same provenance and the same build recipe on both
# architectures, the library is compiled here for either one from the signalapp/libsignal tag
# that matches the bundled libsignal-client jar; the prebuilt upstream binaries are discarded in
# signal-cli-dist. The toolchain image is digest-pinned and runs on the build host (native on
# same-architecture runners, cross-compiling otherwise).
FROM --platform=$BUILDPLATFORM ${RUST_IMAGE} AS libsignal-build

ARG TARGETARCH

COPY docker/libsignal-pins.txt /tmp/libsignal-pins.txt
COPY --from=signal-cli-src /opt/signal-cli/lib/ /tmp/signal-lib/

RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git <<'EOF'
set -eu
case "${TARGETARCH}" in
    amd64)
        target=x86_64-unknown-linux-gnu
        lib=libsignal_jni_amd64.so
        magic='x86-64'
        cross_pkgs='gcc-x86-64-linux-gnu g++-x86-64-linux-gnu libc6-dev-amd64-cross'
        ;;
    arm64)
        target=aarch64-unknown-linux-gnu
        lib=libsignal_jni_aarch64.so
        magic='ARM aarch64'
        cross_pkgs='gcc-aarch64-linux-gnu g++-aarch64-linux-gnu libc6-dev-arm64-cross'
        ;;
    *) echo "Unsupported TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;;
esac
host="$(rustc -vV | sed -n 's|host: ||p')"
# The cross toolchain is only needed when the build host is not the target architecture.
if [ "$host" = "$target" ]; then cross_pkgs=""; fi

apt-get update
# shellcheck disable=SC2086  # cross_pkgs is a deliberate word list
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git clang libclang-dev cmake make protobuf-compiler libprotobuf-dev python3 file \
    $cross_pkgs
rm -rf /var/lib/apt/lists/*

# Version of the libsignal that the verified signal-cli release bundles
jar="$(ls /tmp/signal-lib/libsignal-client-*.jar)"
version="$(basename "$jar" .jar)"; version="${version#libsignal-client-}"
pin="$(awk -v v="$version" '$1 == v { print $2 }' /tmp/libsignal-pins.txt)"
test -n "$pin" || { echo "No pinned commit for libsignal $version in docker/libsignal-pins.txt" >&2; exit 1; }

git init -q /src && cd /src
git fetch -q --depth 1 https://github.com/signalapp/libsignal "refs/tags/v${version}"
test "$(git rev-parse 'FETCH_HEAD^{commit}')" = "$pin" || { echo "libsignal v${version} does not match the pinned commit ${pin}" >&2; exit 1; }
git checkout -q --detach FETCH_HEAD

# The toolchain image must provide exactly the Rust release libsignal pins.
test "$(rustc --version | cut -d' ' -f2)" = "$(cat rust-toolchain)"
if [ "$host" != "$target" ]; then rustup target add "$target"; fi

# Dependencies resolve strictly from the committed Cargo.lock (checksummed registry crates,
# git dependencies pinned by commit).
cargo fetch --locked
CARGO_BUILD_TARGET="$target" java/build_jni.sh desktop
git diff --exit-code -- Cargo.lock

mkdir /out
cp "java/client/src/main/resources/${lib}" /out/
file "/out/${lib}" | grep -q "$magic"
echo "libsignal ${version} (${pin}) built for ${target}"
EOF

FROM scratch AS libsignal-native
COPY --from=libsignal-build /out/ /

# ──────────────────────────────────────────────────────────────
# Stage 2b-ii: signal-cli distribution trimmed to one architecture
# ──────────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS signal-cli-dist

ARG TARGETARCH

# binutils (strip) exists only in this stage and never reaches the runtime image.
RUN apt-get update && apt-get upgrade -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends binutils \
 && rm -rf /var/lib/apt/lists/*

COPY --from=signal-cli-src /opt/signal-cli /opt/signal-cli
COPY --from=libsignal-native / /libsignal-native/

# The release archive was verified in signal-cli-src. The two bundled multi-platform
# native jars are cut down to the target architecture (libsignal-client carries macOS/Windows
# and prebuilt amd64 libraries, which are replaced by the source-built one; sqlite-jdbc carries
# ~20 platforms) and the libsignal debug sections are stripped (the symbol table stays); a
# stripped library behaves identically.
RUN <<'EOF'
set -eu
python3 - <<'PY'
import os, platform, subprocess, zipfile

# --- trim native libraries to the target architecture (after verification) ---
arch = os.environ.get("TARGETARCH") or {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]
libsignal_arch, sqlite_arch = {"amd64": ("amd64", "x86_64"), "arm64": ("aarch64", "aarch64")}[arch]
libsignal_keep = f"libsignal_jni_{libsignal_arch}.so"
sqlite_keep = f"org/sqlite/native/Linux/{sqlite_arch}/"
NATIVE_SUFFIXES = (".so", ".dylib", ".dll", ".jnilib")


def strip_debug(data):
    lib = "/tmp/native.so"
    with open(lib, "wb") as fh:
        fh.write(data)
    subprocess.run(["strip", "--strip-debug", lib], check=True)
    with open(lib, "rb") as fh:
        data = fh.read()
    os.remove(lib)
    return data


def rewrite(jar, keep, add=()):
    tmp = jar + ".tmp"
    kept = []
    with zipfile.ZipFile(jar) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as dst:
        for info in src.infolist():
            if not keep(info.filename):
                continue
            dst.writestr(info, src.read(info))
            kept.append(info.filename)
        for name, path in add:
            with open(path, "rb") as fh:
                dst.writestr(name, strip_debug(fh.read()))
            kept.append(name)
    with zipfile.ZipFile(tmp) as check:
        if check.testzip() is not None:
            raise SystemExit(f"Corrupt jar after rewrite: {jar}")
    os.replace(tmp, jar)
    return kept


libdir = "/opt/signal-cli/lib"


def find_jar(prefix):
    (jar,) = [f for f in os.listdir(libdir) if f.startswith(prefix) and f.endswith(".jar")]
    return os.path.join(libdir, jar)


# libsignal-client: classes plus exactly one native library, the one built from source in
# libsignal-build (every upstream prebuilt library is dropped)
built = f"/libsignal-native/{libsignal_keep}"
if not os.path.isfile(built):
    raise SystemExit(f"libsignal-build produced no {libsignal_keep}")
kept = rewrite(
    find_jar("libsignal-client-"),
    lambda n: not n.endswith(NATIVE_SUFFIXES),
    add=[(libsignal_keep, built)],
)
if kept.count(libsignal_keep) != 1 or sum(n.endswith(NATIVE_SUFFIXES) for n in kept) != 1:
    raise SystemExit(f"libsignal-client does not contain exactly {libsignal_keep}")
print(f"libsignal-client: installed {libsignal_keep} built from source")

# sqlite-jdbc: only the glibc Linux library of the target architecture
kept = rewrite(
    find_jar("sqlite-jdbc-"),
    lambda n: not n.startswith("org/sqlite/native/") or n.startswith(sqlite_keep),
)
if not any(n.startswith(sqlite_keep) for n in kept):
    raise SystemExit(f"sqlite-jdbc has no native library for {sqlite_keep}")
print(f"Trimmed signal-cli native libraries to {arch}")
PY
EOF

# ──────────────────────────────────────────────────────────────
# Stage 2c: Java runtime for signal-cli (jlink, only the modules signal-cli needs)
# ──────────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS jre-builder

# LTS major signal-cli is built for (class version 69); the newest patch release of it
# is installed from the Adoptium repository at build time.
ARG JAVA_MAJOR

# binutils: jlink --strip-debug calls objcopy. Build-only, never copied to the runtime.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gpg \
    binutils \
 && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public -o /tmp/adoptium.key \
 && gpg --import --import-options show-only /tmp/adoptium.key 2>&1 | grep -q "3B04D753C9050D9A5D343F39843C48A565F8F04B" \
 && gpg --dearmor -o /usr/share/keyrings/adoptium.gpg /tmp/adoptium.key \
 && rm /tmp/adoptium.key \
 && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo "$VERSION_CODENAME") main" > /etc/apt/sources.list.d/adoptium.list \
 && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "temurin-${JAVA_MAJOR}-jdk" \
 && rm -rf /var/lib/apt/lists/*

COPY --from=signal-cli-dist /opt/signal-cli /opt/signal-cli

# Module list: jdeps over all signal-cli classes (java.desktop = ImageIO/java.beans, jdk.httpserver =
# daemon HTTP, jdk.net/jdk.security.auth = unix sockets/D-Bus, jdk.unsupported = Unsafe, java.sql =
# Jackson/sqlite-jdbc, java.net.http, java.management, java.naming, java.xml, java.instrument,
# java.compiler) plus modules loaded only via services/reflection: jdk.charsets, jdk.crypto.ec,
# jdk.crypto.cryptoki (TLS/crypto providers), jdk.zipfs, jdk.naming.dns, jdk.management.
# jdk.localedata (+10 MB) is kept for locale compatibility. Left out on purpose: jgss, rmi, jfr, jdwp.
RUN set -eu; \
    modules="java.base,java.compiler,java.desktop,java.instrument,java.logging,java.management,java.naming,java.net.http,java.sql,java.xml,jdk.charsets,jdk.crypto.cryptoki,jdk.crypto.ec,jdk.httpserver,jdk.localedata,jdk.management,jdk.naming.dns,jdk.net,jdk.security.auth,jdk.unsupported,jdk.zipfs"; \
    jlink --add-modules "$modules" --output /opt/java \
          --strip-debug --no-man-pages --no-header-files --compress zip-6; \
    # Guard: fail the build if a signal-cli update needs a module that is not in the runtime.
    mkdir /tmp/classes && cd /tmp/classes; \
    for jar in /opt/signal-cli/lib/*.jar; do jar xf "$jar"; done; \
    find . -name module-info.class -delete; rm -rf META-INF/versions; \
    deps="$(jdeps --ignore-missing-deps --print-module-deps .)"; \
    test -n "$deps"; \
    needed="$(printf '%s' "$deps" | tr ',' '\n')"; \
    /opt/java/bin/java --list-modules | sed 's/@.*//' | sort > /tmp/have; \
    for m in $needed; do grep -qx "$m" /tmp/have || { echo "jlink runtime lacks required module $m" >&2; exit 1; }; done; \
    /opt/java/bin/java -version; \
    # Traceability + guard: the runtime must belong to the requested LTS line.
    grep -E "^JAVA_VERSION=\"${JAVA_MAJOR}[.\"]" /opt/java/release

# ──────────────────────────────────────────────────────────────
# Stage 3: Runtime
# ──────────────────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS runtime

# OCI Labels (set via --build-arg or defaults)
ARG APP_VERSION=dev
ARG BUILD_DATE
ARG VCS_REF
ARG TARGETPLATFORM
ARG JAVA_MAJOR

LABEL maintainer="Gill-Bates" \
      org.opencontainers.image.title="justUp!" \
      org.opencontainers.image.description="Lightweight Uptime Monitor" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.platform="${TARGETPLATFORM}" \
      org.opencontainers.image.source="https://github.com/Gill-Bates/justUp" \
      org.opencontainers.image.licenses="MIT" \
      io.justup.java.major="${JAVA_MAJOR}"

# Runtime deps - single RUN to minimize layers
# 1. Install the runtime packages (the Java runtime comes from the jre-builder stage)
# 2. Security: Use capabilities instead of SUID for ping, remove SUID bit
# 3. Generate the en_US.UTF-8 locale

RUN apt-get update && apt-get upgrade -y && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    libfreetype6 \
    libjpeg62-turbo \
    zlib1g \
    mtr-tiny \
    whatweb \
    gosu \
    iputils-ping \
    libcap2-bin \
    locales \
 && printf '#!/bin/sh\nexec /usr/local/bin/signal-cli --config "${SIGNAL_DATA_DIR:-/app/data/signal}" "$@"\n' \
     > /usr/local/bin/signal \
 && chmod +x /usr/local/bin/signal \
 # Security: Grant raw socket capability for ping and mtr (required as non-root)
 && setcap cap_net_raw+ep /bin/ping \
 && setcap cap_net_raw+ep /usr/bin/mtr \
 && chmod u-s /bin/ping \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
 && locale-gen

# Trimmed Java runtime (jlink) built from the verified Temurin JDK; no JDK in this image.
COPY --from=jre-builder /opt/java /opt/java
RUN ln -s /opt/java/bin/java /usr/local/bin/java \
 && java -version

# signal-cli arrives pre-trimmed from the signal-cli-dist stage (symlink + smoke test here).
COPY --from=signal-cli-dist /opt/signal-cli /opt/signal-cli
RUN ln -s /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli \
 && signal-cli --version \
 && { grep '^JAVA_VERSION=' /opt/java/release; signal-cli --version; \
      ls /opt/signal-cli/lib | grep '^libsignal-client-'; } > /opt/COMPONENTS.txt

# Non-root user with dedicated home directory (security: avoid HOME=/tmp)
RUN groupadd -r justup && useradd -r -g justup justup \
 && mkdir -p /home/justup/.matplotlib \
 && chown -R justup:justup /home/justup

WORKDIR /app

# Python dependencies from clean-deps (build-only tooling removed)
COPY --from=clean-deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# The base image ships pip/setuptools/wheel; nothing at runtime installs packages,
# so drop them (and their vendored libraries) from the final image.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/lib/python3.13/site-packages/_distutils_hack* \
           /usr/local/lib/python3.13/site-packages/pkg_resources* \
           /usr/local/lib/python3.13/site-packages/distutils-precedence.pth \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 /usr/local/bin/wheel

# The service writes under /app/data; source files stay read-only to its UID.
COPY --from=app-source /build/app/ /src/app/
# app/utils/version.py reads [project] version from /src/pyproject.toml
COPY --from=app-source /build/pyproject.toml /src/
# app/utils/version.py reads the commit from /src/BUILD_INFO (.dockerignore keeps
# the host file out of the context, so it is written from the VCS_REF build arg)
RUN printf '%s\n' "${VCS_REF:-dev}" > /src/BUILD_INFO
# COPY keeps the build-context modes (git does not track directory modes); a 0700
# directory such as app/static/img would be unreadable for the non-root runtime UID.
# Normalise to world-readable, owner-writable-only.
RUN chmod -R u=rwX,go=rX /src/app

# Country flags load at runtime from a public CDN (flag-icons on jsDelivr),
# so no flag SVGs are baked into the image; see fragments/traceroute_map.html.

# Copy app metadata files (for auditability / license compliance, not used at runtime)
COPY LICENSE CHANGELOG.md /app/

# CLI tool already included in app/ from app-source - create symlink only
RUN chmod +x /src/app/justup-cli \
 && ln -s /src/app/justup-cli /usr/local/bin/justup-cli

# ─── DATA DIRECTORIES ──────────────────────────────────────────
RUN mkdir -p \
      /app/data \
      /app/data/sqlite \
      /app/data/tsdb \
      /app/data/pdf_reports \
      /app/data/signal \
      /app/data/geoip \
 && chown -R justup:justup /app/data

# Entrypoint performs UID/GID setup, then hands off to gosu.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8000

# Health-Check using Python (curl removed from runtime for security)
# NOTE: Spawns Python interpreter (~30 MB RSS) per check. Acceptable at 30s intervals.
# For lower overhead, use: CMD ["/src/app/justup-cli", "healthcheck"] if implemented.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; r=urllib.request.urlopen(f'http://localhost:{os.environ.get(\"JUSTUP_PORT\",8000)}/healthz',timeout=3); sys.exit(0 if r.status==200 else 1)"

# Environment variables
# Security: Use dedicated home directory instead of /tmp
# Flexibility: Split JAVA heap sizes for easier runtime customization
# Structure: /src/app/ = Python package, /app/ = workdir with data/config
ENV HOME=/home/justup \
    MPLCONFIGDIR=/home/justup/.matplotlib \
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
    JUSTUP_PORT=8000 \
    JUSTUP_DATA_DIR=/app/data \
    SIGNAL_CLI_PATH=/usr/local/bin/signal-cli \
    SIGNAL_DATA_DIR=/app/data/signal \
    JAVA_XMS=64m \
    JAVA_XMX=256m

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
