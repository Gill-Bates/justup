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
# Stage 2a: Nmap (official stable release, built from the signature-verified source)
# ──────────────────────────────────────────────────────────────
# Both architectures build the same upstream source natively with identical configure options
# (no vendor binary, no Debian package): one recipe, one provenance format, one set of runtime
# libraries from this base. The official x86-64 RPM was dropped on purpose: it is a statically
# linked vendor build with its own OpenSSL, so amd64 would differ from arm64 in options and TLS
# stack and the image would depend on a binary package we cannot rebuild.
#
# The release workflow resolves the latest stable release once (tools/nmap-release.py latest),
# downloads and verifies it once (fetch) and passes the same version and archive hash to both
# builds. The args are mandatory and carry no default: they are part of every BuildKit cache key
# here, so a new release or a changed upstream archive can never hit a cached old binary, and the
# builder aborts if the archive it downloads does not match NMAP_SOURCE_SHA256.
# Local build:
#   v="$(python3 tools/nmap-release.py latest)"
#   python3 tools/nmap-release.py fetch "$v" /tmp/nmap-verify   # prints nothing, writes provenance.txt
#   h="$(sed -n 's/^source_sha256=//p' /tmp/nmap-verify/provenance.txt)"
#   docker build --target runtime --build-arg "NMAP_VERSION=$v" --build-arg "NMAP_SOURCE_SHA256=$h" -f docker/Dockerfile .
FROM ${PYTHON_BASE} AS nmap-builder
ARG TARGETARCH
ARG NMAP_VERSION
ARG NMAP_SOURCE_SHA256
COPY tools/nmap-release.py /usr/local/bin/nmap-release.py
# Everything below exists only in this stage: compiler, headers, gpg, the source tree.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates gnupg bzip2 build-essential libssl-dev pkg-config zlib1g-dev libpcre2-dev \
 && rm -rf /var/lib/apt/lists/*
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN <<'EOF'
set -euo pipefail
version="${NMAP_VERSION:-}"
source_sha256="${NMAP_SOURCE_SHA256:-}"
if [[ -z "$version" || -z "$source_sha256" || -z "${TARGETARCH:-}" ]]; then
  echo "NMAP_VERSION, NMAP_SOURCE_SHA256 and TARGETARCH are required (see the stage comment)" >&2
  exit 1
fi
case "$TARGETARCH" in
  amd64) machine="Advanced Micro Devices X86-64" ;;
  arm64) machine="AArch64" ;;
  *) echo "Unsupported TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;;
esac
python3 /usr/local/bin/nmap-release.py fetch "$version" /tmp/nmap "$source_sha256"
tar -xjf "/tmp/nmap/nmap-${version}.tar.bz2" -C /tmp/nmap
cd "/tmp/nmap/nmap-${version}"
# PCRE2 and zlib come from Debian (dpkg-owned, so the image scan sees them). libssh2 only serves NSE,
# which is not built. libpcap, libdnet and liblinear stay bundled (see docs/development/release-image.md).
./configure --prefix=/usr --with-libpcap=included --with-libdnet=included --with-liblinear=included \
  --without-libssh2 \
  --without-liblua --without-ncat --without-nping --without-ndiff --without-zenmap
make -j"$(nproc)" nmap
make DESTDIR=/out install-nmap

# Only what scans need: the binary and the data files (-sV needs the probes; no NSE, no man pages).
find /out/usr/share/nmap -maxdepth 1 -type f \
  ! -name nmap-services ! -name nmap-service-probes ! -name nmap-protocols ! -name nmap-rpc -delete
rm -rf /out/usr/share/nmap/scripts /out/usr/share/nmap/nselib /out/usr/share/man
for data in nmap-services nmap-service-probes nmap-protocols nmap-rpc; do
  test -s "/out/usr/share/nmap/${data}"
done

# ELF contract of the shipped binary: architecture, loader, no RPATH/RUNPATH, all libraries resolvable.
readelf -h /out/usr/bin/nmap | grep -q "Machine:.*${machine}"
readelf -l /out/usr/bin/nmap | grep -q "Requesting program interpreter"
if readelf -d /out/usr/bin/nmap | grep -Eq 'RPATH|RUNPATH'; then
  echo "nmap carries an RPATH/RUNPATH" >&2; exit 1
fi
mkdir -p /out/usr/share/doc/nmap
ldd /out/usr/bin/nmap > /out/usr/share/doc/nmap/DEPENDENCIES.txt
# "! cmd" never trips "set -e", so every negative check is an explicit if.
if grep -q 'not found' /out/usr/share/doc/nmap/DEPENDENCIES.txt; then
  echo "nmap has unresolved libraries" >&2; exit 1
fi
if grep -Eq '/tmp/|/out/|/build' /out/usr/share/doc/nmap/DEPENDENCIES.txt; then
  echo "nmap resolves a library from the build tree" >&2; exit 1
fi
# A silent autodetect fallback to the bundled copies would hide them from the scan: fail instead.
# PCRE2 is a direct NEEDED entry. zlib is not: the linker drops libz.so.1 from nmap's own list and it
# resolves only through libcrypto, so zlib is proven by the banner (Debian: "libz-<v>", bundled:
# "nmap-libz-<v>") plus the Debian library in the ldd output.
readelf -d /out/usr/bin/nmap | grep -q 'NEEDED.*libpcre2-8.so.0'
banner="$(NMAPDIR=/out/usr/share/nmap /out/usr/bin/nmap --version)"
compiled_with="$(sed -n 's/^Compiled with: //p' <<<"$banner")"
for required in 'libz-[0-9]' 'libpcre2-[0-9]'; do
  grep -Eq "(^| )${required}" <<<"$compiled_with" \
    || { echo "nmap banner lacks the Debian library ${required}: ${compiled_with}" >&2; exit 1; }
done
if grep -Eq 'nmap-libz|nmap-libpcre2|libssh2' <<<"$compiled_with"; then
  echo "nmap bundles a library that must come from Debian: ${compiled_with}" >&2; exit 1
fi
grep -Eq '^[[:space:]]*libz\.so\.1 => /' /out/usr/share/doc/nmap/DEPENDENCIES.txt
# Versions of the libraries compiled into the binary, read from the verified source tree.
libpcap_version="$(cat libpcap/VERSION.txt)"
libdnet_version="$(sed -n "s/^PACKAGE_VERSION='\(.*\)'$/\1/p" libdnet-stripped/configure)"
liblinear_version="$(awk '/#define LIBLINEAR_VERSION/ {printf "%d.%02d", $3/100, $3%100}' liblinear/linear.h)"
for v in "$libpcap_version" "$libdnet_version" "$liblinear_version"; do
  [[ "$v" =~ ^[0-9]+(\.[0-9]+)+$ ]] || { echo "Cannot read a bundled library version (got '${v}')" >&2; exit 1; }
done
actual="$(sed -n 's/^Nmap version \([^ ]*\).*/\1/p' <<<"$banner")"
[[ "$actual" == "$version" ]] || { echo "Built Nmap ${actual} differs from the resolved ${version}" >&2; exit 1; }

# Licence, provenance and the notice (NPSL redistribution) travel with the binary.
tar -xOf "/tmp/nmap/nmap-${version}.tar.bz2" "nmap-${version}/LICENSE" > /out/usr/share/doc/nmap/LICENSE
{
  cat /tmp/nmap/provenance.txt
  printf 'architecture=%s\nbuild_method=verified-source\nbinary_sha256=%s\n' \
    "$TARGETARCH" "$(sha256sum /out/usr/bin/nmap | cut -d' ' -f1)"
  printf 'runtime_dependencies=%s\n' "$(awk '/=>/ {print $1}' /out/usr/share/doc/nmap/DEPENDENCIES.txt | sort | paste -sd, -)"
  printf 'bundled_libraries=libdnet=%s,liblinear=%s,libpcap=%s\n' "$libdnet_version" "$liblinear_version" "$libpcap_version"
  printf 'bundled_libraries_origin=compiled into the binary from the verified source tarball (invisible to package scanners)\n'
} > /out/usr/share/doc/nmap/PROVENANCE.txt
cat > /out/usr/share/doc/nmap/NOTICE.txt <<NOTICE
This image redistributes Nmap ${version} (nmap binary, nmap-services, nmap-service-probes,
nmap-protocols, nmap-rpc) under the Nmap Public Source License (NPSL), https://nmap.org/npsl/.
The licence text is in LICENSE next to this file. Nmap is a registered trademark of Nmap Software LLC.
Source of this release: https://nmap.org/dist/nmap-${version}.tar.bz2 (signature verified,
SHA-256 ${source_sha256})
Build: unmodified upstream source, built natively for ${TARGETARCH}; provenance: PROVENANCE.txt
NOTICE
EOF

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

apt-get update && apt-get upgrade -y # Keep this! This is wanted!
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
 && fingerprints="$(gpg --show-keys --with-colons /tmp/adoptium.key | awk -F: '$1=="pub"{p=1;next} $1=="sub"{p=0} $1=="fpr"&&p{print $10;p=0}')" \
 && test "$fingerprints" = "3B04D753C9050D9A5D343F39843C48A565F8F04B" \
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
    libssl3 \
    libstdc++6 \
    libzstd1 \
    libpcre2-8-0 \
    whatweb \
    gosu \
    iputils-ping \
    libcap2-bin \
    locales \
 && printf '#!/bin/sh\nexec /usr/local/bin/signal-cli --config "${SIGNAL_DATA_DIR:-/app/data/signal}" "$@"\n' \
     > /usr/local/bin/signal \
 && chmod +x /usr/local/bin/signal \
 # Security: Grant raw socket capability for ping (required as non-root)
 && setcap cap_net_raw+ep /bin/ping \
 && chmod u-s /bin/ping \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
 && locale-gen

# Trimmed Java runtime (jlink) built from the verified Temurin JDK; no JDK in this image.
COPY --from=jre-builder /opt/java /opt/java
# The JVM leaves /tmp/hsperfdata_* behind after each start; removed in the same layer.
RUN ln -s /opt/java/bin/java /usr/local/bin/java \
 && java -version \
 && rm -rf /tmp/hsperfdata_*

# signal-cli arrives pre-trimmed from the signal-cli-dist stage (symlink + smoke test here).
COPY --from=signal-cli-dist /opt/signal-cli /opt/signal-cli
RUN ln -s /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli \
 && signal-cli --version \
 && { grep '^JAVA_VERSION=' /opt/java/release; signal-cli --version; \
      ls /opt/signal-cli/lib | grep '^libsignal-client-'; } > /opt/COMPONENTS.txt \
 && rm -rf /tmp/hsperfdata_*

COPY --from=nmap-builder /out/usr/bin/nmap /usr/bin/nmap
COPY --from=nmap-builder /out/usr/share/nmap /usr/share/nmap
COPY --from=nmap-builder /out/usr/share/doc/nmap /usr/share/doc/nmap
RUN test -s /usr/share/nmap/nmap-services \
 && test -s /usr/share/nmap/nmap-service-probes \
 && nmap --version \
 && ldd /usr/bin/nmap > /usr/share/doc/nmap/DEPENDENCIES.txt \
 && ! grep -q 'not found' /usr/share/doc/nmap/DEPENDENCIES.txt \
 && builder_dependencies="$(sed -n 's/^runtime_dependencies=//p' /usr/share/doc/nmap/PROVENANCE.txt)" \
 && runtime_dependencies="$(awk '/=>/ {print $1}' /usr/share/doc/nmap/DEPENDENCIES.txt | sort | paste -sd, -)" \
 && { test "$builder_dependencies" = "$runtime_dependencies" \
      || { echo "Nmap libraries differ between builder (${builder_dependencies}) and runtime (${runtime_dependencies})" >&2; exit 1; }; }
# Debian packages (name=version) that own every library the binary resolves here: the part of Nmap an image scan covers.
# The library list (runtime_dependencies, from the builder) was compared with this stage's ldd result above.
RUN set -eu \
 && libraries="$(awk '/=>/ && $3 ~ /^\// {print $3}' /usr/share/doc/nmap/DEPENDENCIES.txt)" \
 && test -n "$libraries" \
 && package_names="$(for library in $libraries; do \
      real_library="$(readlink -f "$library")" || exit 1; \
      owner="$(dpkg-query -S "$real_library")" || exit 1; \
      case "$owner" in *:*) package="${owner%%: *}" ;; *) echo "Cannot parse package owner for $real_library: $owner" >&2; exit 1 ;; esac; \
      test -n "$package" || { echo "No Debian package owns $real_library" >&2; exit 1; }; \
      printf '%s\n' "$package"; \
    done \
    )" \
 && test -n "$package_names" \
 && package_versions="$(for package in $package_names; do \
      dpkg-query -W -f='${binary:Package}=${Version}\n' "$package" || exit 1; \
    done)" \
 && test -n "$package_versions" \
 && system_libraries="$(printf '%s\n' "$package_versions" | sort -u | paste -sd, -)" \
 && test -n "$system_libraries" \
 && printf 'system_libraries=%s\n' "$system_libraries" \
      >> /usr/share/doc/nmap/PROVENANCE.txt

# Non-root user with dedicated home directory (security: avoid HOME=/tmp)
RUN groupadd -r justup && useradd -r -g justup justup \
 && mkdir -p /home/justup/.matplotlib \
 && chown -R justup:justup /home/justup
# Dedicated identity of the Nmap worker (docker/entrypoint.sh): the only process that gets
# CAP_NET_RAW. The web user joins its group solely to reach the worker's Unix socket.
RUN groupadd -r -g 10001 justup-nmap \
 && useradd -r -u 10001 -g justup-nmap -d /nonexistent -s /usr/sbin/nologin justup-nmap \
 && usermod -aG justup-nmap justup \
 && command -v setpriv >/dev/null

WORKDIR /app

# Python dependencies from clean-deps (build-only tooling removed)
COPY --from=clean-deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# The base image ships pip/setuptools/wheel; nothing at runtime installs packages,
# so drop them (and their vendored libraries, plus the ensurepip pip wheel that
# could re-bootstrap pip) from the final image.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip* \
           /usr/local/lib/python3.13/site-packages/setuptools* \
           /usr/local/lib/python3.13/site-packages/wheel* \
           /usr/local/lib/python3.13/site-packages/_distutils_hack* \
           /usr/local/lib/python3.13/site-packages/pkg_resources* \
           /usr/local/lib/python3.13/site-packages/distutils-precedence.pth \
           /usr/local/lib/python3.13/ensurepip/_bundled \
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
COPY docker/nmap-worker.sh /usr/local/bin/nmap-worker.sh
RUN chmod 0755 /entrypoint.sh /usr/local/bin/nmap-worker.sh

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
    NMAPDIR=/usr/share/nmap \
    JAVA_XMS=64m \
    JAVA_XMX=256m

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "app"]
