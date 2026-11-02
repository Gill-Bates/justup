#!/usr/bin/env python3
#
# tests/test_release_newest.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release privileges (cleanup, 'latest') derived from published tags."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_newest", Path(__file__).resolve().parents[1] / "tools" / "release-newest.py"
)
release_newest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_newest)


ARCHS = frozenset({"amd64", "arm64"})


def _decide(published, current):
    is_newest, moves_latest, _, _ = release_newest.decide(set(published), current, ARCHS)
    return is_newest, moves_latest


@pytest.mark.parametrize(
    ("published", "current", "expected"),
    [
        # A newer prerelease must not block 'latest' for a newer stable 1.x.
        (["1.9.0", "2.0.0-rc.1"], "1.9.1", (False, True)),
        (["1.9.0", "2.0.0-rc.1", "1.9.1"], "1.9.1", (False, True)),
        (["1.9.1", "2.0.0-rc.1"], "2.0.0-rc.1", (True, False)),
        (["1.9.1", "2.0.0-rc.1"], "2.0.0", (True, True)),
        (["1.9.1", "2.0.0"], "1.9.1", (False, False)),
        (["1.9.1", "2.0.0", "1.9.2-amd64"], "1.9.2", (False, False)),
        (["1.9.0", "1.9.1-arm64", "latest"], "1.9.1", (True, True)),
        (["2.0.0-rc.10"], "2.0.0-rc.2", (False, False)),
        ([], "1.0.0", (True, True)),
    ],
)
def test_release_privileges(published, current, expected):
    assert _decide(published, current) == expected


def test_unparseable_current_version_fails_closed():
    with pytest.raises(ValueError):
        release_newest.decide(set(), "latest", ARCHS)


def test_suffix_outside_the_matrix_is_a_prerelease_not_a_fragment():
    # Only the built architectures are fragments; anything else counts as a
    # release, which can only block pruning, never allow it.
    assert _decide(["1.9.2-armv7"], "1.9.1") == (False, True)


def test_main_requires_architectures(tmp_path, capsys):
    tags = tmp_path / "tags.txt"
    tags.write_text("1.0.0\n", encoding="utf-8")
    assert release_newest.main(["x", str(tags), "1.0.0"]) == 1
    assert release_newest.main(["x", str(tags), "1.0.0", "amd64", "arm64"]) == 0
    assert "is_newest=true" in capsys.readouterr().out


def test_main_fails_closed_on_unparseable_version(tmp_path, capsys):
    tags = tmp_path / "tags.txt"
    tags.write_text("1.0.0\n", encoding="utf-8")
    assert release_newest.main(["x", str(tags), "latest", "amd64"]) == 1
    assert capsys.readouterr().out == ""
