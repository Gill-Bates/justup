#!/usr/bin/env python3
#
# tools/verify-libsignal-native.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Check that the libsignal-client jar packages a native library for the target architecture.

This confirms packaging and architecture (entry name, non-empty, ELF header). With ``--load`` it
also dlopen()s the extracted library (RTLD_NOW), which catches an unresolved glibc symbol or a
missing shared dependency, i.e. a build/runtime base mismatch. It does NOT run JNI_OnLoad or
prove that the JVM loads the library; a real Signal send stays a manual gate.

docker-build.yml pipes this script into the finished image
(``python - <arch> < tools/verify-libsignal-native.py``).

Usage: verify-libsignal-native.py [--load] {amd64|arm64} [lib-dir]
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_LIB_DIR = Path("/opt/signal-cli/lib")
ELFCLASS64 = 2
ELFDATA2LSB = 1
# arch -> (jar entry name, e_machine, machine label)
EXPECTED = {
    "amd64": ("libsignal_jni_amd64.so", 62, "EM_X86_64"),
    "arm64": ("libsignal_jni_aarch64.so", 183, "EM_AARCH64"),
}


class CheckError(Exception):
    """Raised when the packaged native library does not meet the expectation."""


def find_jar(lib_dir: Path) -> Path:
    jars = sorted(lib_dir.glob("libsignal-client-*.jar"))
    if len(jars) != 1:
        raise CheckError(f"expected exactly one libsignal-client-*.jar in {lib_dir}, found {len(jars)}")
    return jars[0]


def check(jar: Path, arch: str) -> str:
    """Validate the jar for ``arch`` and return a one-line description."""
    if arch not in EXPECTED:
        raise CheckError(f"unsupported architecture '{arch}' (expected one of {sorted(EXPECTED)})")
    name, machine, label = EXPECTED[arch]
    with zipfile.ZipFile(jar) as archive:
        matches = [info for info in archive.infolist() if info.filename == name]
        if len(matches) != 1:
            raise CheckError(f"{jar.name}: expected exactly one entry '{name}', found {len(matches)}")
        data = archive.read(matches[0])
    if not data:
        raise CheckError(f"{jar.name}: entry '{name}' is empty")
    if len(data) < 20 or data[:4] != b"\x7fELF":
        raise CheckError(f"{name}: not an ELF file (bad magic)")
    if data[4] != ELFCLASS64:
        raise CheckError(f"{name}: EI_CLASS={data[4]}, expected {ELFCLASS64} (ELF64)")
    if data[5] != ELFDATA2LSB:
        raise CheckError(f"{name}: EI_DATA={data[5]}, expected {ELFDATA2LSB} (little endian)")
    found = int.from_bytes(data[18:20], "little")
    if found != machine:
        raise CheckError(f"{name}: e_machine={found}, expected {machine} ({label})")
    return f"{name} size={len(data)} machine={found} ({label})"


def load(jar: Path, arch: str) -> str:
    """dlopen the packaged library with immediate symbol binding; must run on the target architecture."""
    name = EXPECTED[arch][0]
    with tempfile.TemporaryDirectory() as directory, zipfile.ZipFile(jar) as archive:
        path = archive.extract(name, directory)
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_LOCAL | os.RTLD_NOW)
        except OSError as error:
            raise CheckError(f"{name}: dlopen failed: {error}") from error
    return f"{name} loads (dlopen, RTLD_NOW)"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--load"]
    if len(args) not in (1, 2):
        print(__doc__, file=sys.stderr)
        return 2
    lib_dir = Path(args[1]) if len(args) == 2 else DEFAULT_LIB_DIR
    try:
        jar = find_jar(lib_dir)
        print(f"OK: {check(jar, args[0])}")
        if "--load" in argv:
            print(f"OK: {load(jar, args[0])}")
    except (CheckError, zipfile.BadZipFile, OSError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
