#!/usr/bin/env python3
#
# tests/test_release_publish.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""tools/release-publish.py: draft-first release decisions and asset verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_publish", ROOT / "tools" / "release-publish.py")
tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tool
_spec.loader.exec_module(tool)

TAG = "v1.2.3"
FILES = {"a.txt": b"alpha", "b.json": b'{"b": 1}', "c.asc": b"signature"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def expected_file(tmp_path):
    directory = tmp_path / "assets"
    directory.mkdir()
    for name, data in FILES.items():
        (directory / name).write_bytes(data)
    out = tmp_path / "expected.json"
    assert tool.main(["x", "manifest", str(out), str(directory), *FILES]) == 0
    return out


def asset(name, asset_id, data=None, **override):
    data = FILES[name] if data is None else data
    item = {"id": asset_id, "name": name, "state": "uploaded", "size": len(data), "digest": f"sha256:{digest(data)}"}
    return {**item, **override}


def release(draft, assets, tag=TAG, release_id=7):
    return {"id": release_id, "tag_name": tag, "draft": draft, "assets": assets}


ALL = [asset("a.txt", 1), asset("b.json", 2), asset("c.asc", 3)]


def expected(expected_file):
    return tool.load_expected(expected_file)


def test_manifest_hashes_exactly_the_named_files(expected_file):
    entries = {e["name"]: e for e in json.loads(expected_file.read_text(encoding="utf-8"))["assets"]}
    assert entries["a.txt"] == {"name": "a.txt", "sha256": digest(b"alpha"), "size": 5}


@pytest.mark.parametrize("problem", ["missing", "extra", "empty", "unsafe-name"])
def test_manifest_refuses_incomplete_or_unclean_asset_directories(tmp_path, problem):
    directory = tmp_path / "assets"
    directory.mkdir()
    for name, data in FILES.items():
        (directory / name).write_bytes(data)
    names = list(FILES)
    if problem == "missing":
        (directory / "a.txt").unlink()
    elif problem == "extra":
        (directory / "stray.bin").write_bytes(b"x")
    elif problem == "empty":
        (directory / "a.txt").write_bytes(b"")
    else:
        names.append("../escape")
    assert tool.main(["x", "manifest", str(tmp_path / "m.json"), str(directory), *names]) == 1


def test_no_release_yet_means_create_everything(expected_file):
    result = tool.plan([[release(False, ALL, tag="v0.0.1")]], TAG, expected(expected_file))
    assert result["state"] == "create" and result["upload"] == ["a.txt", "b.json", "c.asc"]


def test_draft_without_the_required_assets_is_resumed_not_published(expected_file):
    stale = asset("b.json", 2, data=b"old", state="uploaded")
    leftover = asset("old.bin", 9, data=b"x")
    result = tool.plan([[release(True, [asset("a.txt", 1), stale, leftover])]], TAG, expected(expected_file))
    assert result["state"] == "draft" and result["release_id"] == 7
    assert result["upload"] == ["b.json", "c.asc"]
    assert {d["name"] for d in result["delete"]} == {"b.json", "old.bin"}


def test_complete_published_release_is_left_untouched(expected_file):
    result = tool.plan([release(False, ALL)], TAG, expected(expected_file))
    assert result == {"state": "published-complete", "release_id": 7, "delete": [], "upload": []}


@pytest.mark.parametrize(
    ("assets", "fragment"),
    [
        (ALL[:2], "missing asset c.asc"),
        ([*ALL, asset("extra.txt", 4, data=b"e")], "unexpected asset extra.txt"),
        ([ALL[0], ALL[1], asset("c.asc", 3, data=b"other")], "c.asc: size"),
        ([ALL[0], ALL[1], asset("c.asc", 3, digest="sha256:" + "0" * 64)], "c.asc: digest"),
        ([ALL[0], ALL[1], asset("c.asc", 3, state="starter")], "state is 'starter'"),
        ([ALL[0], ALL[1], ALL[2], ALL[2]], "duplicate asset c.asc"),
    ],
)
def test_published_release_with_wrong_assets_is_refused_and_never_modified(expected_file, assets, fragment):
    with pytest.raises(tool.ReleaseError, match="not modified") as error:
        tool.plan([release(False, assets)], TAG, expected(expected_file))
    assert fragment in str(error.value)


def test_two_releases_for_one_tag_are_refused(expected_file):
    with pytest.raises(tool.ReleaseError, match="2 releases"):
        tool.plan([release(True, [], release_id=1), release(False, ALL, release_id=2)], TAG, expected(expected_file))


def test_plan_cli_reads_a_slurped_listing_and_reports_refusal(tmp_path, expected_file, capsys):
    listing = tmp_path / "releases.json"
    listing.write_text(json.dumps([[release(False, ALL[:1])]]), encoding="utf-8")
    assert tool.main(["x", "plan", str(listing), TAG, str(expected_file)]) == 1
    assert "missing asset b.json" in capsys.readouterr().err


def verify(tmp_path, expected_file, rel, *extra):
    path = tmp_path / "release.json"
    path.write_text(json.dumps(rel), encoding="utf-8")
    return tool.main(["x", "verify", str(path), str(expected_file), *extra])


def test_verify_accepts_matching_draft_and_published_state(tmp_path, expected_file):
    assert verify(tmp_path, expected_file, release(True, ALL), "--draft") == 0
    assert verify(tmp_path, expected_file, release(False, ALL), "--published") == 0


def test_verify_rejects_release_in_the_wrong_state(tmp_path, expected_file):
    assert verify(tmp_path, expected_file, release(False, ALL), "--draft") == 1
    assert verify(tmp_path, expected_file, release(True, ALL), "--published") == 1


@pytest.mark.parametrize(
    "assets",
    [
        ALL[:2],
        [ALL[0], ALL[1], asset("c.asc", 3, data=b"short")],
        [ALL[0], ALL[1], asset("c.asc", 3, digest="sha256:" + "f" * 64)],
    ],
)
def test_verify_rejects_missing_wrong_size_and_wrong_digest(tmp_path, expected_file, assets):
    assert verify(tmp_path, expected_file, release(True, assets), "--draft") == 1


def test_verify_downloads_catch_bytes_that_differ_despite_matching_metadata(tmp_path, expected_file):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    for name, data in FILES.items():
        (downloads / name).write_bytes(data)
    args = ("--draft", "--downloads", str(downloads))
    assert verify(tmp_path, expected_file, release(True, ALL), *args) == 0
    (downloads / "b.json").write_bytes(b'{"b": 2}')
    assert verify(tmp_path, expected_file, release(True, ALL), *args) == 1


def test_asset_without_api_digest_needs_a_downloaded_copy(tmp_path, expected_file):
    undigested = [{k: v for k, v in a.items() if k != "digest"} for a in ALL]
    assert verify(tmp_path, expected_file, release(True, undigested), "--draft") == 1
    downloads = tmp_path / "dl"
    downloads.mkdir()
    for name, data in FILES.items():
        (downloads / name).write_bytes(data)
    assert verify(tmp_path, expected_file, release(True, undigested), "--draft", "--downloads", str(downloads)) == 0


@pytest.fixture
def source(tmp_path):
    version, data = "7.98", b"archive bytes"
    directory = tmp_path / "src"
    directory.mkdir()
    (directory / f"nmap-{version}.tar.bz2").write_bytes(data)
    (directory / f"nmap-{version}.tar.bz2.asc").write_bytes(b"sig")
    (directory / "nmap_gpgkeys.txt").write_bytes(b"keys")
    provenance = tmp_path / "prov.txt"
    provenance.write_text(f"nmap_version={version}\nsource_sha256={digest(data)}\n", encoding="utf-8")
    return directory, version, digest(data), provenance


def test_prepared_source_is_accepted_before_the_push(source):
    directory, version, sha, provenance = source
    tool.check_source(directory, version, sha, [provenance])


@pytest.mark.parametrize("break_it", ["hash", "signature", "provenance", "empty"])
def test_prepared_source_that_disagrees_blocks_the_push(source, break_it):
    directory, version, sha, provenance = source
    if break_it == "hash":
        (directory / f"nmap-{version}.tar.bz2").write_bytes(b"tampered")
    elif break_it == "signature":
        (directory / f"nmap-{version}.tar.bz2.asc").unlink()
    elif break_it == "provenance":
        provenance.write_text(f"nmap_version={version}\nsource_sha256={'0' * 64}\n", encoding="utf-8")
    else:
        (directory / "nmap_gpgkeys.txt").write_bytes(b"")
    with pytest.raises(tool.ReleaseError):
        tool.check_source(directory, version, sha, [provenance])
