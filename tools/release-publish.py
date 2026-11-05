#!/usr/bin/env python3
#
# tools/release-publish.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Decisions and checks for the atomic GitHub release: draft first, verify every asset, then publish.

The workflow does the GitHub calls (``gh api``); this tool decides and verifies, so the rules are
tested. The expected asset list is built from the local files and is the single source of truth for
names, sizes and SHA-256 values.

  source   <dir> <version> <sha256> <provenance>...   before the registry push: the prepared Nmap
                                                      archive, its signature and the manifest inputs
                                                      exist and match the hash the images were built from
  manifest <out.json> <dir> <name>...                 hash the files in <dir>; the directory must hold
                                                      exactly the named, non-empty files
  plan     <releases.json> <tag> <expected.json>      what to do with the release for <tag>; prints JSON
  verify   <release.json> <expected.json> (--draft | --published) [--downloads DIR]
                                                      every asset uploaded, size and digest as expected,
                                                      nothing unexpected, release in the stated state

``plan`` states: ``create`` (no release for the tag), ``draft`` (resume: upload what is missing, replace
what differs), ``published-complete`` (nothing to change). A published release with a missing, wrong or
extra asset is an error: it is never edited, deleted or re-tagged. A release's assets count as verified
only when the API reports ``uploaded`` and the size equals the local size; the API digest, when
present, must equal the local SHA-256, and with ``--downloads`` the bytes read back from GitHub must
hash to it (required when the API reports no digest).

Standard library only. Exit 0 ok, 1 refused or mismatch, 2 usage.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SHA256_RE = re.compile(r"[0-9a-f]{64}")
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class ReleaseError(Exception):
    """The release must not proceed or must not be touched."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReleaseError(f"{path}: cannot read JSON ({error})") from error


def build_manifest(directory: Path, names: list[str]) -> dict:
    if not names or len(set(names)) != len(names) or not all(NAME_RE.fullmatch(n) for n in names):
        raise ReleaseError("asset names must be unique and file-name safe")
    if not directory.is_dir():
        raise ReleaseError(f"{directory}: not a directory")
    present = {p.name for p in directory.iterdir()}
    if missing := sorted(set(names) - present):
        raise ReleaseError(f"missing release assets: {', '.join(missing)}")
    if extra := sorted(present - set(names)):
        raise ReleaseError(f"unexpected files in the release asset directory: {', '.join(extra)}")
    assets = []
    for name in sorted(names):
        path = directory / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ReleaseError(f"{name}: not a regular, non-empty file")
        assets.append({"name": name, "sha256": file_sha256(path), "size": path.stat().st_size})
    return {"assets": assets}


def load_expected(path: Path) -> dict[str, dict]:
    data = read_json(path)
    items = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ReleaseError(f"{path}: no expected assets")
    expected: dict[str, dict] = {}
    for item in items:
        valid = (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("sha256"), str)
            and SHA256_RE.fullmatch(item["sha256"])
            and isinstance(item.get("size"), int)
            and not isinstance(item["size"], bool)
            and item["size"] > 0
        )
        if not valid or item["name"] in expected:
            raise ReleaseError(f"{path}: malformed or repeated expected asset")
        expected[item["name"]] = item
    return expected


def asset_problems(release: dict, expected: dict[str, dict], downloads: Path | None = None) -> list[str]:
    """Differences between the assets GitHub holds and the expected ones; empty means identical."""
    raw = release.get("assets")
    if not isinstance(raw, list) or not all(isinstance(a, dict) for a in raw):
        raise ReleaseError("release carries no readable asset list")
    names = [a.get("name") for a in raw]
    problems = [f"duplicate asset {n}" for n in sorted({n for n in names if names.count(n) > 1})]
    held = {a["name"]: a for a in raw if isinstance(a.get("name"), str)}
    problems += [f"missing asset {name}" for name in sorted(set(expected) - set(held))]
    problems += [f"unexpected asset {name}" for name in sorted(set(held) - set(expected))]
    for name in sorted(set(expected) & set(held)):
        asset, want = held[name], expected[name]
        if asset.get("state") != "uploaded":
            problems.append(f"{name}: state is {asset.get('state')!r}, expected 'uploaded'")
        if asset.get("size") != want["size"]:
            problems.append(f"{name}: size {asset.get('size')!r} differs from the expected {want['size']}")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{want['sha256']}":
            problems.append(f"{name}: digest {digest!r} differs from the expected sha256:{want['sha256']}")
        if downloads is not None or digest is None:
            copy = downloads / name if downloads is not None else None
            if copy is None or not copy.is_file():
                problems.append(f"{name}: no API digest and no downloaded copy to hash")
            elif file_sha256(copy) != want["sha256"]:
                problems.append(f"{name}: downloaded bytes do not hash to the expected sha256:{want['sha256']}")
    return problems


def releases_for_tag(releases_json, tag: str) -> list[dict]:
    """`gh api --paginate --slurp` yields a list of pages; a plain list of releases works as well."""
    if not isinstance(releases_json, list):
        raise ReleaseError("release listing is not a JSON list")
    flat = [r for page in releases_json for r in (page if isinstance(page, list) else [page])]
    if not all(isinstance(r, dict) for r in flat):
        raise ReleaseError("release listing holds a non-object entry")
    return [r for r in flat if r.get("tag_name") == tag]


def plan(releases_json, tag: str, expected: dict[str, dict]) -> dict:
    matches = releases_for_tag(releases_json, tag)
    if not matches:
        return {"state": "create", "release_id": None, "delete": [], "upload": sorted(expected)}
    if len(matches) > 1:
        raise ReleaseError(f"{len(matches)} releases carry the tag {tag}; resolve this by hand")
    release = matches[0]
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        raise ReleaseError(f"release for {tag} has no numeric id")
    if release.get("draft") is False:
        problems = asset_problems(release, expected)
        if problems:
            raise ReleaseError(
                f"{tag} is already published with different assets and is not modified: " + "; ".join(problems)
            )
        return {"state": "published-complete", "release_id": release_id, "delete": [], "upload": []}
    if release.get("draft") is not True:
        raise ReleaseError(f"release for {tag} reports no draft state")
    held = release.get("assets")
    if not isinstance(held, list):
        raise ReleaseError("draft release carries no readable asset list")
    delete, keep = [], set()
    for asset in held:
        name, want = asset.get("name"), expected.get(asset.get("name"))
        identical = (
            want is not None
            and asset.get("state") == "uploaded"
            and asset.get("size") == want["size"]
            and asset.get("digest") in (None, f"sha256:{want['sha256']}")
        )
        if identical:
            keep.add(name)
        else:
            delete.append({"id": asset.get("id"), "name": name})
    return {
        "state": "draft",
        "release_id": release_id,
        "delete": delete,
        "upload": sorted(set(expected) - keep),
    }


def check_source(directory: Path, version: str, sha256: str, provenance: list[Path]) -> None:
    """Before the push: the verified archive is prepared and agrees with what the images were built from."""
    if not VERSION_RE.fullmatch(version) or not SHA256_RE.fullmatch(sha256):
        raise ReleaseError("invalid version or SHA-256")
    if not provenance:
        raise ReleaseError("at least one image provenance file is required")
    archive = directory / f"nmap-{version}.tar.bz2"
    for required in (archive, directory / f"{archive.name}.asc", directory / "nmap_gpgkeys.txt"):
        if not required.is_file() or required.stat().st_size == 0:
            raise ReleaseError(f"prepared Nmap source file missing or empty: {required}")
    if file_sha256(archive) != sha256:
        raise ReleaseError("prepared Nmap source archive does not match the hash the images were built with")
    for path in provenance:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise ReleaseError(f"{path}: cannot read provenance ({error})") from error
        if f"source_sha256={sha256}" not in lines or f"nmap_version={version}" not in lines:
            raise ReleaseError(f"{path.name}: provenance does not name the prepared archive")


def main(argv: list[str]) -> int:
    args = argv[1:]
    try:
        if len(args) >= 4 and args[0] == "manifest":
            Path(args[1]).write_text(
                json.dumps(build_manifest(Path(args[2]), args[3:]), indent=2) + "\n", encoding="utf-8"
            )
            return 0
        if len(args) >= 5 and args[0] == "source":
            check_source(Path(args[1]), args[2], args[3], [Path(a) for a in args[4:]])
            return 0
        if len(args) == 4 and args[0] == "plan":
            print(json.dumps(plan(read_json(Path(args[1])), args[2], load_expected(Path(args[3])))))
            return 0
        if len(args) in (4, 6) and args[0] == "verify" and args[3] in ("--draft", "--published"):
            downloads = None
            if len(args) == 6:
                if args[4] != "--downloads":
                    print(__doc__, file=sys.stderr)
                    return 2
                downloads = Path(args[5])
            release = read_json(Path(args[1]))
            if not isinstance(release, dict) or release.get("draft") is not (args[3] == "--draft"):
                raise ReleaseError(f"release is not in the state {args[3][2:]}")
            if problems := asset_problems(release, load_expected(Path(args[2])), downloads):
                raise ReleaseError("; ".join(problems))
            return 0
    except (ReleaseError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
