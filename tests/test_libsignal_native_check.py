#!/usr/bin/env python3
#
# tests/test_libsignal_native_check.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""tools/verify-libsignal-native.py against synthetic libsignal-client jars."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("verify_libsignal_native", ROOT / "tools" / "verify-libsignal-native.py")
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)

AMD64 = "libsignal_jni_amd64.so"
AARCH64 = "libsignal_jni_aarch64.so"
HOST_LIB_DIRS = ("/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu", "/usr/lib64", "/lib64")


def elf(machine: int, *, klass: int = 2, data: int = 1, magic: bytes = b"\x7fELF") -> bytes:
    header = bytearray(magic + bytes([klass, data, 1]) + bytes(9))
    header += struct.pack("<HH", 3, machine)
    return bytes(header) + b"payload"


def make_jar(lib_dir: Path, entries: list[tuple[str, bytes]]) -> Path:
    lib_dir.mkdir(exist_ok=True)
    jar = lib_dir / "libsignal-client-0.1.0.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return jar


def test_matching_library_passes_and_other_platforms_are_ignored(tmp_path):
    jar = make_jar(tmp_path, [(AMD64, elf(62)), (AARCH64, elf(183)), ("libsignal_jni_amd64.dylib", b"x")])
    assert "machine=62" in tool.check(jar, "amd64")
    assert "machine=183" in tool.check(jar, "arm64")


def test_only_the_other_architecture_present_fails(tmp_path):
    jar = make_jar(tmp_path, [(AARCH64, elf(183))])
    with pytest.raises(tool.CheckError, match="found 0"):
        tool.check(jar, "amd64")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (elf(183), "e_machine"),
        (elf(62, klass=1), "EI_CLASS"),
        (elf(62, data=2), "EI_DATA"),
        (elf(62, magic=b"MZ\x90\x00"), "bad magic"),
    ],
)
def test_invalid_library_content_fails(tmp_path, content, message):
    jar = make_jar(tmp_path, [(AMD64, content)])
    with pytest.raises(tool.CheckError, match=message):
        tool.check(jar, "amd64")


def test_duplicate_entry_fails(tmp_path):
    with pytest.warns(UserWarning):
        jar = make_jar(tmp_path, [(AMD64, elf(62)), (AMD64, elf(62))])
    with pytest.raises(tool.CheckError, match="found 2"):
        tool.check(jar, "amd64")


def test_unsupported_architecture_fails(tmp_path):
    with pytest.raises(tool.CheckError, match="unsupported"):
        tool.check(make_jar(tmp_path, [(AMD64, elf(62))]), "riscv64")


def test_jar_must_be_unique(tmp_path):
    with pytest.raises(tool.CheckError, match="found 0"):
        tool.find_jar(tmp_path)
    make_jar(tmp_path, [(AMD64, elf(62))])
    (tmp_path / "libsignal-client-0.2.0.jar").write_bytes(b"")
    with pytest.raises(tool.CheckError, match="found 2"):
        tool.find_jar(tmp_path)


def test_main_exit_codes(tmp_path, capsys):
    make_jar(tmp_path, [(AMD64, elf(62))])
    assert tool.main(["x", "amd64", str(tmp_path)]) == 0
    assert "OK:" in capsys.readouterr().out
    assert tool.main(["x", "arm64", str(tmp_path)]) == 1
    assert "FAIL:" in capsys.readouterr().err


def test_load_check_dlopens_a_real_library_and_rejects_an_unloadable_one(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    with pytest.raises(tool.CheckError, match="dlopen failed"):
        tool.load(make_jar(broken, [(AMD64, elf(62) + b"not loadable")]), "amd64")
    real = next((Path(d) / "libm.so.6" for d in HOST_LIB_DIRS if (Path(d) / "libm.so.6").is_file()), None)
    if real is None:
        pytest.skip("no host libm.so.6 to load")
    good = tmp_path / "good"
    good.mkdir()
    assert "loads" in tool.load(make_jar(good, [(AMD64, real.read_bytes())]), "amd64")


def test_main_load_flag_fails_on_unloadable_library(tmp_path, capsys):
    make_jar(tmp_path, [(AMD64, elf(62))])
    assert tool.main(["x", "--load", "amd64", str(tmp_path)]) == 1
    assert "dlopen failed" in capsys.readouterr().err
