#!/usr/bin/env python3
#
# tests/test_runtime_image_purity.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""tools/verify-runtime-image.py against synthetic root file systems and image archives."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("verify_runtime_image", ROOT / "tools" / "verify-runtime-image.py")
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


def touch(root: Path, relative: str, content: bytes = b"x") -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def make_image_root(root: Path) -> Path:
    """A minimal clean runtime tree with the required scanner and native tools."""
    touch(root, "opt/signal-cli/bin/signal-cli")
    touch(root, "opt/java/bin/java")
    for path in (
        "usr/bin/nmap",
        "usr/share/nmap/nmap-services",
        "usr/share/nmap/nmap-service-probes",
        "usr/share/doc/nmap/LICENSE",
        "usr/share/doc/nmap/PROVENANCE.txt",
    ):
        touch(root, path)
    touch(root, "usr/local/bin/python3")
    touch(root, "usr/lib/x86_64-linux-gnu/libz.so.1")
    touch(root, "usr/local/lib/python3.13/site-packages/numpy/_core/lib/libnpymath.a")
    touch(root, "usr/local/lib/python3.13/config-3.13-x86_64-linux-gnu/python.o")
    (root / "tmp").mkdir()
    (root / "usr/src").mkdir(parents=True)
    jar = root / "opt/signal-cli/lib/libsignal-client-0.1.0.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("libsignal_jni_amd64.so", b"\x7fELF")
        archive.writestr("org/signal/Example.class", b"class")
    return root


def problems(root: Path, arch: str = "amd64") -> list[str]:
    return tool.missing_required(root, arch) + tool.tree_violations(root)


def test_clean_image_passes_and_required_native_libraries_are_not_flagged(tmp_path):
    assert problems(make_image_root(tmp_path)) == []


@pytest.mark.parametrize(
    "relative",
    [
        "usr/bin/gcc",
        "usr/bin/g++",
        "usr/bin/cc",
        "usr/bin/x86_64-linux-gnu-gcc-14",
        "usr/bin/clang-19",
        "usr/bin/cmake",
        "usr/bin/make",
        "usr/local/bin/cargo",
        "usr/local/cargo/bin/rustc",
        "opt/java/bin/javac",
        "opt/java/bin/jlink",
        "root/.cargo/registry/index",
        "root/.rustup/settings.toml",
        "root/.cache/pip/http/a",
        "home/justup/.cache/pip/a",
        "src/.git/HEAD",
        "app/.pytest_cache/v/cache",
        "usr/lib/jvm/temurin-25/bin/java",
        "build/pyproject.toml",
        "src/libsignal/Cargo.lock",
        "usr/lib/foo.o",
        "opt/target/release/deps/libx.rlib",
        "usr/lib/libfoo.a",
        "root/.docker/config.json",
        "root/.netrc",
    ],
)
def test_build_tool_cache_source_and_object_artifacts_fail(tmp_path, relative):
    root = make_image_root(tmp_path)
    touch(root, relative)
    found = problems(root)
    assert found and any("/" + relative.split("/")[0] in entry or relative in entry for entry in found), found


@pytest.mark.parametrize("directory", ["tmp", "var/tmp", "usr/src"])
def test_leftovers_in_scratch_directories_fail(tmp_path, directory):
    root = make_image_root(tmp_path)
    touch(root, f"{directory}/hsperfdata_root/12")
    assert any("leftovers" in entry for entry in problems(root))


def test_tool_names_outside_bin_directories_are_not_flagged(tmp_path):
    root = make_image_root(tmp_path)
    touch(root, "usr/share/doc/make")
    touch(root, "usr/lib/python3/dist-packages/jar")
    assert problems(root) == []


@pytest.mark.parametrize(
    "missing",
    ["opt/signal-cli/bin/signal-cli", "opt/java/bin/java", "usr/bin/nmap", "usr/share/nmap/nmap-service-probes"],
)
def test_missing_required_file_fails(tmp_path, missing):
    root = make_image_root(tmp_path)
    (root / missing).unlink()
    assert any("required file is missing" in entry for entry in problems(root))


def test_libsignal_jar_without_native_library_fails(tmp_path):
    root = make_image_root(tmp_path)
    with zipfile.ZipFile(root / "opt/signal-cli/lib/libsignal-client-0.1.0.jar", "w") as archive:
        archive.writestr("org/signal/Example.class", b"class")
    assert any("libsignal_jni" in entry for entry in problems(root))


def test_unsupported_architecture_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        tool.missing_required(make_image_root(tmp_path), "riscv64")


def build_image_archive(path: Path, layers: list[dict[str, bytes | None]]) -> None:
    """A docker-save-shaped archive; a value of None marks a whiteout (deletion)."""
    names = []
    with tarfile.open(path, "w") as image:
        for number, files in enumerate(layers):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as layer:
                for name, content in files.items():
                    directory, _, base = name.rpartition("/")
                    data_ = b"" if content is None else content
                    info = tarfile.TarInfo(name if content is not None else f"{directory}/.wh.{base}")
                    info.size = len(data_)
                    layer.addfile(info, io.BytesIO(data_))
            data = buffer.getvalue()
            layer_name = f"blobs/layer{number}.tar"
            info = tarfile.TarInfo(layer_name)
            info.size = len(data)
            image.addfile(info, io.BytesIO(data))
            names.append(layer_name)
        manifest = json.dumps([{"Layers": names}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        image.addfile(info, io.BytesIO(manifest))


def test_layer_scan_finds_artifact_that_a_later_layer_deleted(tmp_path):
    archive = tmp_path / "image.tar"
    build_image_archive(
        archive,
        [{"usr/lib/libz.so.1": b"x"}, {"usr/bin/gcc": b"x", "root/.cargo/env": b"x"}, {"usr/bin/gcc": None}],
    )
    found = tool.layer_violations(archive)
    assert any(entry.startswith("layer 2: /usr/bin/gcc") for entry in found), found
    assert any("/root/.cargo" in entry for entry in found), found


def test_layer_scan_passes_clean_layers(tmp_path):
    archive = tmp_path / "image.tar"
    build_image_archive(archive, [{"usr/lib/libz.so.1": b"x"}])
    assert tool.layer_violations(archive) == []


def test_layer_scan_finds_scratch_artifact_even_when_a_later_layer_deletes_it(tmp_path):
    archive = tmp_path / "image.tar"
    build_image_archive(archive, [{"tmp/build-output.tar.gz": b"artifact"}, {"tmp/build-output.tar.gz": None}])
    found = tool.layer_violations(archive)
    assert any(entry == "layer 1: /tmp/build-output.tar.gz: scratch-directory artifact" for entry in found), found


def test_main_exit_codes(tmp_path, capsys):
    root = make_image_root(tmp_path)
    assert tool.main(["x", "fs", "amd64", str(root)]) == 0
    assert "OK:" in capsys.readouterr().out
    touch(root, "usr/bin/gcc")
    assert tool.main(["x", "fs", "amd64", str(root)]) == 1
    assert "/usr/bin/gcc" in capsys.readouterr().err
    assert tool.main(["x", "layers", str(tmp_path / "missing.tar")]) == 2
    assert tool.main(["x"]) == 2
