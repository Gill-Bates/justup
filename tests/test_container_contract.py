#!/usr/bin/env python3
#
# tests/test_container_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Container contract: entrypoint drops privileges without a root fallback; Dockerfile wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests import _support

ROOT = _support.ROOT
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def test_entrypoint_exists_and_parses():
    assert ENTRYPOINT.is_file()
    assert not (ROOT / "docker" / "docker-entrypoint.sh").exists()
    subprocess.run(["bash", "-n", str(ENTRYPOINT)], check=True)


def test_entrypoint_requires_gosu_and_ends_with_privilege_drop():
    text = ENTRYPOINT.read_text()
    assert "command -v gosu" in text
    assert text.rstrip().splitlines()[-1].strip() == 'exec gosu justup "$@"'
    # No branch may exec the application as root.
    assert [line for line in text.splitlines() if line.strip().startswith("exec ") and "gosu" not in line] == []


def test_dockerfile_uses_renamed_entrypoint_and_pyproject_is_single_manifest():
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    assert "docker/entrypoint.sh" in dockerfile
    assert "docker-entrypoint.sh" not in dockerfile
    for forbidden in ("VERSION", "requirements.txt"):
        assert not (Path(ROOT) / forbidden).exists()


def test_dockerfile_writes_build_info_from_vcs_ref():
    """.dockerignore blocks the host BUILD_INFO, so the image must create it itself."""
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text()
    ignore = (ROOT / ".dockerignore").read_text()
    assert "BUILD_INFO" not in ignore
    assert "> /src/BUILD_INFO" in dockerfile
    assert dockerfile.index("ARG VCS_REF") < dockerfile.index("> /src/BUILD_INFO")
