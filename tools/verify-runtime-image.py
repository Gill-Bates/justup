#!/usr/bin/env python3
#
# tools/verify-runtime-image.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check that a justUp runtime image carries no build tools or build leftovers (standard library only).

Contract. Allowed: the Python interpreter and modules, native libraries, signal-cli and its JARs,
the jlink Java runtime, the application and its runtime configuration, package metadata.
Forbidden: compilers and build tools (gcc, g++, cc, clang, cmake, make, cargo, rustc, rustup, javac,
jar, jlink, jdeps, jshell, autoconf, automake, libtool, rpm tools, gpg) in a bin directory,
Nmap source archives, RPMs, signatures and unpacked source trees, libtool archives (.la),
the mtr tools (mtr, mtr-packet and the mtr-tiny package entry; traceroute runs through Nmap),
Cargo/rustup/pip/Maven caches, a full JDK, .git
directories, build source trees (Cargo.toml/Cargo.lock), object files (.o, .rlib, .obj and static
.a archives), test caches, build credentials, and leftovers in /tmp, /var/tmp and /usr/src.
Exceptions that are part of the shipped runtime, not of the build: static .a archives inside
site-packages (numpy ships libnpymath.a) and objects in the CPython config-* directory of the
base interpreter. pip, setuptools and wheel must be gone from the final file system (``fs``); the
base image still carries them in its own layer, which the layer scan accepts by design (see
package_tooling). Compiled shared libraries (.so) are never forbidden; the required
libsignal JNI library is asserted instead.

  fs <amd64|arm64> [root]   inspect the file system of a running container; docker-build.yml pipes
                            this file into the image (``python - fs amd64 < tools/...``).
  layers <image.tar>        inspect every layer of a ``docker save`` archive, so an artifact that
                            a later layer deleted is still found. Whiteout markers are ignored;
                            files beneath /tmp and /var/tmp are reported because a later deletion
                            does not remove their bytes from an earlier layer.

Exit code 0: clean, 1: violations (listed on stderr), 2: usage or unreadable input.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

TOOL_NAME = re.compile(
    r"^(?:[a-z0-9_]+-linux-gnu-)?(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+|cmake|make|cargo|rustc|rustup|rustdoc"
    r"|javac|jar|jlink|jdeps|jshell|autoconf|automake|autoreconf|aclocal|libtool|libtoolize|rpm|rpm2cpio|gpg|gpgv)"
    r"(?:-[0-9][0-9.]*)?$"
)
BIN_DIRS = frozenset({"bin", "sbin"})
MTR_TOOL = re.compile(r"^mtr(?:-packet|-tiny)?$")
FORBIDDEN_PATHS = (
    "/root/.cargo",
    "/root/.rustup",
    "/root/.cache",
    "/root/.m2",
    "/root/.gradle",
    "/root/.docker",
    "/root/.ssh",
    "/root/.netrc",
    "/root/.git-credentials",
    "/usr/local/cargo",
    "/usr/local/rustup",
    "/usr/lib/jvm",
    "/build",
)
FORBIDDEN_DIR_NAMES = frozenset({".git", ".cache", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".gnupg"})
# Nmap build leftovers: release archives, signatures, key files, an unpacked source tree, libtool archives.
NMAP_ARTIFACT = re.compile(
    r"^nmap-[0-9][0-9.]*.*\.(?:tar(?:\.[a-z0-9]+)?|tgz|rpm|asc)$|^nmap_gpgkeys\.txt$|^provenance\.txt$"
)
NMAP_SOURCE_DIR = re.compile(r"^nmap-[0-9][0-9.]*$")
FORBIDDEN_FILE_NAMES = frozenset({"Cargo.toml", "Cargo.lock"})
OBJECT_SUFFIXES = (".o", ".rlib", ".obj")
STATIC_SUFFIX = ".a"
SITE_PACKAGES = re.compile(r"^/usr/local/lib/python3[0-9.]*/site-packages/")
CPYTHON_CONFIG = re.compile(r"^/usr/local/lib/python3[0-9.]*/config-[^/]+/")
# Directories that must be empty in a fresh container.
EMPTY_DIRS = ("/tmp", "/var/tmp", "/usr/src")  # noqa: S108 # paths inside the audited image
SKIPPED_ROOT_DIRS = frozenset({"proc", "sys", "dev"})
LIBSIGNAL_JAR_DIR = "/opt/signal-cli/lib"
REQUIRED_FILES = (
    "/opt/signal-cli/bin/signal-cli",
    "/opt/java/bin/java",
    "/usr/bin/nmap",
    "/usr/share/nmap/nmap-services",
    "/usr/share/nmap/nmap-service-probes",
    "/usr/share/doc/nmap/LICENSE",
    "/usr/share/doc/nmap/PROVENANCE.txt",
    "/usr/share/doc/nmap/NOTICE.txt",
)
ARCHS = frozenset({"amd64", "arm64"})
MAX_LISTED = 50


def violation(path: str, is_dir: bool = False) -> str | None:
    """Return the reason ``path`` (absolute, POSIX) breaks the contract, or None."""
    posix = PurePosixPath(path)
    name = posix.name
    for forbidden in FORBIDDEN_PATHS:
        if path == forbidden or path.startswith(forbidden + "/"):
            return f"forbidden location {forbidden}"
    if is_dir:
        if name in FORBIDDEN_DIR_NAMES:
            return f"forbidden directory {name}"
        return f"unpacked Nmap source tree {name}" if NMAP_SOURCE_DIR.match(name) else None
    if NMAP_ARTIFACT.match(name):
        return f"Nmap build leftover {name}"
    if name.endswith(".la"):
        return f"libtool archive {name}"
    if posix.parent.name in BIN_DIRS and TOOL_NAME.match(name):
        return f"build tool {name}"
    if posix.parent.name in BIN_DIRS and MTR_TOOL.match(name):
        return f"removed tool {name} (traceroute uses Nmap)"
    if name in FORBIDDEN_FILE_NAMES:
        return f"build source file {name}"
    if name.endswith(OBJECT_SUFFIXES) and not CPYTHON_CONFIG.match(path):
        return f"object file {name}"
    if name.endswith(STATIC_SUFFIX) and not SITE_PACKAGES.match(path):
        return f"static archive {name}"
    return None


def tree_violations(root: Path) -> list[str]:
    """Walk a directory tree (not following symlinks) and report contract violations."""
    found: list[str] = []
    for current, dirs, files in os.walk(root):
        relative = Path(current).relative_to(root)
        if not relative.parts:
            dirs[:] = [d for d in dirs if d not in SKIPPED_ROOT_DIRS]
        base = "/" + relative.as_posix() if relative.parts else ""
        for entry, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
            path = f"{base}/{entry}"
            reason = violation(path, is_dir)
            if reason:
                found.append(f"{path}: {reason}")
    found.extend(package_tooling(root))
    found.extend(mtr_package_entries(root))
    for directory in EMPTY_DIRS:
        target = root / directory.lstrip("/")
        if target.is_dir():
            leftovers = sorted(item.name for item in target.iterdir())
            if leftovers:
                found.append(f"{directory}: leftovers {', '.join(leftovers[:5])}")
    return found


def mtr_package_entries(root: Path) -> list[str]:
    """The dpkg database must not list the removed mtr-tiny package."""
    found: list[str] = []
    status = root / "var/lib/dpkg/status"
    if status.is_file() and re.search(rb"^Package: mtr-tiny\s*$", status.read_bytes(), re.MULTILINE):
        found.append("/var/lib/dpkg/status: package mtr-tiny is installed")
    found.extend(
        f"/var/lib/dpkg/info/{entry.name}: package mtr-tiny is installed"
        for entry in sorted((root / "var/lib/dpkg/info").glob("mtr-tiny*"))
    )
    return found


def package_tooling(root: Path) -> list[str]:
    """Python package tooling must not be visible in the final file system.

    The base image ships pip, setuptools and wheel in its own layer; the Dockerfile removes them
    in a later layer. That hides them from the running image but leaves their bytes in the base
    layer, so this is a file-system check only and the layer scan deliberately does not flag them.
    """
    found: list[str] = []
    for site in sorted(root.glob("usr/local/lib/python3*/site-packages")):
        found.extend(
            f"/{entry.relative_to(root).as_posix()}: Python package tooling is still visible"
            for entry in sorted(site.iterdir())
            if re.match(r"^(?:pip|setuptools|wheel|pkg_resources)(?:-[0-9][^/]*\.(?:dist|egg)-info)?$", entry.name)
        )
    found.extend(
        f"/usr/local/bin/{entry.name}: Python package tooling is still visible"
        for entry in sorted((root / "usr/local/bin").glob("*"))
        if re.match(r"^(?:pip[0-9.]*|wheel)$", entry.name)
    )
    return found


def missing_required(root: Path, arch: str) -> list[str]:
    """Return the required runtime files that are absent."""
    if arch not in ARCHS:
        raise ValueError(f"unsupported architecture '{arch}' (expected one of {sorted(ARCHS)})")
    found = [f"{r}: required file is missing" for r in REQUIRED_FILES if not (root / r.lstrip("/")).is_file()]
    lib_dir = root / LIBSIGNAL_JAR_DIR.lstrip("/")
    jars = sorted(lib_dir.glob("libsignal-client-*.jar"))
    if len(jars) != 1:
        return [*found, f"{LIBSIGNAL_JAR_DIR}: expected one libsignal-client jar, found {len(jars)}"]
    # The ELF header and the exact library name per architecture belong to verify-libsignal-native.py.
    with zipfile.ZipFile(jars[0]) as archive:
        natives = [i for i in archive.infolist() if re.fullmatch(r"libsignal_jni_[a-z0-9_]+\.so", i.filename)]
    if len(natives) != 1 or natives[0].file_size == 0:
        found.append(f"{jars[0].name}: expected one non-empty libsignal_jni_*.so for {arch}, found {len(natives)}")
    return found


def layer_violations(image_tar: Path) -> list[str]:
    """Report every contract violation in any layer of a ``docker save`` archive."""
    found: list[str] = []
    with tarfile.open(image_tar) as image:
        member = image.getmember("manifest.json")
        manifest = json.load(image.extractfile(member))
        if len(manifest) != 1:
            raise ValueError(f"expected one image in the archive, found {len(manifest)}")
        for number, layer_name in enumerate(manifest[0]["Layers"], start=1):
            layer_file = image.extractfile(layer_name)
            with tarfile.open(fileobj=layer_file, mode="r|*") as layer:
                for entry in layer:
                    # removeprefix, not lstrip("./"): lstrip would turn "./.git" into "/git" and hide it.
                    path = "/" + entry.name.removeprefix("./").strip("/")
                    if PurePosixPath(path).name.startswith(".wh."):
                        continue
                    if path.startswith(("/tmp/", "/var/tmp/")) and not entry.isdir():  # noqa: S108 # paths inside the audited image
                        found.append(f"layer {number}: {path}: scratch-directory artifact")
                        continue
                    reason = violation(path, entry.isdir())
                    if reason:
                        found.append(f"layer {number}: {path}: {reason}")
    return found


def report(problems: list[str], ok_message: str) -> int:
    if not problems:
        print(f"OK: {ok_message}")
        return 0
    print(f"FAIL: {len(problems)} runtime image purity violation(s)", file=sys.stderr)
    for problem in problems[:MAX_LISTED]:
        print(f"  {problem}", file=sys.stderr)
    if len(problems) > MAX_LISTED:
        print(f"  ... and {len(problems) - MAX_LISTED} more", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    try:
        if len(argv) in (3, 4) and argv[1] == "fs":
            root = Path(argv[3]) if len(argv) == 4 else Path("/")
            problems = missing_required(root, argv[2]) + tree_violations(root)
            return report(problems, f"file system of {root} is free of build tools and leftovers ({argv[2]})")
        if len(argv) == 3 and argv[1] == "layers":
            return report(layer_violations(Path(argv[2])), "no layer contains build tools or leftovers")
    except (ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
