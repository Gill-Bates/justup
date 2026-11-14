#!/usr/bin/env python3
#
# tools/nmap-release.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolve Nmap's official stable release and fetch its signature-verified source archive.

latest                           print the version of the latest stable release (nmap.org)
fetch VERSION DIR [SHA256]       download the source archive and its signature into DIR, verify the
                                 signature against the pinned Nmap signing key, optionally require
                                 the archive hash, write DIR/provenance.txt; any failure exits 1
release-assets VERSION SRC_DIR OUT_DIR SHA256 IMAGE_REF PROVENANCE...
                                 release job: re-verify the archive fetched in `meta` (signature, hash
                                 against the value the images were built with, every architecture's
                                 PROVENANCE.txt from the image), then write the archive, its signature
                                 and nmap-source-manifest.txt (URL, SHA-256, NPSL, image digest) to OUT_DIR
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

DOWNLOAD_PAGE = "https://nmap.org/download.html"
DIST = "https://nmap.org/dist"
SIGNING_KEYS = "https://svn.nmap.org/nmap/docs/nmap_gpgkeys.txt"
PROJECT_KEY = "436D66AB9A798425FDA0E3F801AF9F036B9355D0"
BAD_STATUS = ("[GNUPG:] EXPKEYSIG", "[GNUPG:] REVKEYSIG", "[GNUPG:] EXPSIG", "[GNUPG:] BADSIG", "[GNUPG:] ERRSIG")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GPG_TIMEOUT = 120
SOURCE_RE = re.compile(rf"^{re.escape(DIST)}/nmap-([0-9]+(?:\.[0-9]+)+)\.tar\.bz2$")


class StableLink(HTMLParser):
    """Collect the links inside the page's "Latest stable Nmap release tarball" line."""

    def __init__(self) -> None:
        super().__init__()
        self.bold_depth = 0
        self.bold_text: list[str] = []
        self.bold_links: list[str] = []
        self.source_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "b":
            if self.bold_depth == 0:
                self.bold_text = []
                self.bold_links = []
            self.bold_depth += 1
        elif tag == "a" and self.bold_depth:
            self.bold_links.append(dict(attrs).get("href") or "")

    def handle_data(self, data: str) -> None:
        if self.bold_depth:
            self.bold_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "b" or not self.bold_depth:
            return
        self.bold_depth -= 1
        if self.bold_depth == 0 and "Latest stable Nmap release tarball:" in " ".join("".join(self.bold_text).split()):
            self.source_links.extend(self.bold_links)


def latest_release(page: str) -> str:
    """Return the version of the one official stable source tarball announced on the download page."""
    parser = StableLink()
    parser.feed(page)
    versions = [match.group(1) for url in parser.source_links if (match := SOURCE_RE.fullmatch(url))]
    if len(versions) != 1:
        raise ValueError("expected exactly one official stable Nmap source tarball")
    return versions[0]


def open_https(url: str, timeout: int):
    """Open a fixed https://nmap.org or svn.nmap.org URL; any other scheme is refused."""
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "justUp-Nmap-build/1"})  # noqa: S310 # https only
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 # https only, checked above


def download(url: str, path: Path, *, limit: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with open_https(url, 60) as response, path.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"download exceeds size limit: {url}")
            digest.update(chunk)
            target.write(chunk)
    if total == 0:
        raise ValueError(f"download is empty: {url}")
    return digest.hexdigest()


def verify_signature(artifact: Path, signature: Path, keyfile: Path, homedir: Path) -> None:
    homedir.mkdir(mode=0o700, exist_ok=True)
    imported = subprocess.run(
        ["gpg", "--batch", "--no-tty", "--homedir", str(homedir), "--import", str(keyfile)],
        capture_output=True,
        text=True,
        check=False,
        timeout=GPG_TIMEOUT,
    )
    if imported.returncode:
        raise ValueError("could not import the Nmap signing key")
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--no-tty",
            "--homedir",
            str(homedir),
            "--status-fd",
            "1",
            "--verify",
            str(signature),
            str(artifact),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=GPG_TIMEOUT,
    )
    # VALIDSIG ends with the primary key fingerprint, also when a subkey signed.
    valid_signers = [line.split()[-1] for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    bad_status = any(line.startswith(BAD_STATUS) for line in result.stdout.splitlines())
    if result.returncode or bad_status or valid_signers != [PROJECT_KEY]:
        raise ValueError(f"Nmap signature is invalid or was made by an untrusted key: {artifact.name}")


def fetch(version: str, output: Path, expected_sha256: str | None = None) -> None:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid Nmap version")
    if expected_sha256 is not None and not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("invalid expected SHA-256")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    keyfile = output / "nmap_gpgkeys.txt"
    download(SIGNING_KEYS, keyfile, limit=1_000_000)
    source_name = f"nmap-{version}.tar.bz2"
    source = output / source_name
    source_url = f"{DIST}/{source_name}"
    source_hash = download(source_url, source, limit=100_000_000)
    source_sig = output / f"{source_name}.asc"
    download(f"{DIST}/sigs/{source_name}.asc", source_sig, limit=100_000)
    verify_signature(source, source_sig, keyfile, output / "gnupg")
    if expected_sha256 is not None and source_hash != expected_sha256:
        raise ValueError(f"source archive hash {source_hash} differs from the resolved {expected_sha256}")
    lines = [
        f"nmap_version={version}",
        f"source_url={source_url}",
        f"source_sha256={source_hash}",
        f"signing_key_fingerprint={PROJECT_KEY}",
    ]
    (output / "provenance.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


NPSL_URL = "https://nmap.org/npsl/"
IMAGE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*@sha256:[0-9a-f]{64}$")


def read_fields(path: Path) -> dict[str, str]:
    """Parse a key=value provenance file; a missing file or a duplicate key is an error."""
    if not path.is_file():
        raise ValueError(f"missing file: {path}")
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key in fields:
            raise ValueError(f"duplicate field {key} in {path.name}")
        fields[key] = value
    return fields


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def release_assets(
    version: str, source: Path, output: Path, expected_sha256: str, image_ref: str, provenance_files: list[Path]
) -> None:
    """Verify the archive from `meta` against the images, then write the release assets."""
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid Nmap version")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("invalid expected SHA-256")
    if not IMAGE_REF_RE.fullmatch(image_ref):
        raise ValueError("image reference must be <repository>@sha256:<digest>")
    if not provenance_files:
        raise ValueError("at least one image provenance file is required")
    name = f"nmap-{version}.tar.bz2"
    archive, signature, keyfile = source / name, source / f"{name}.asc", source / "nmap_gpgkeys.txt"
    for required in (archive, signature, keyfile):
        if not required.is_file():
            raise ValueError(f"missing file: {required}")
    actual = file_sha256(archive)
    if actual != expected_sha256:
        raise ValueError(
            f"source archive hash {actual} differs from the hash the images were built with {expected_sha256}"
        )
    verify_signature(archive, signature, keyfile, output.parent / f".{output.name}-gnupg")
    source_url = f"{DIST}/{name}"
    architectures: list[str] = []
    for path in provenance_files:
        fields = read_fields(path)
        expected = {
            "nmap_version": version,
            "source_url": source_url,
            "source_sha256": expected_sha256,
            "signing_key_fingerprint": PROJECT_KEY,
        }
        for key, value in expected.items():
            if fields.get(key) != value:
                raise ValueError(f"{path.name}: {key} is {fields.get(key)!r}, expected {value!r}")
        arch = fields.get("architecture", "")
        if not re.fullmatch(r"[a-z0-9_]+", arch) or arch in architectures:
            raise ValueError(f"{path.name}: architecture {arch!r} is missing, invalid or repeated")
        architectures.append(arch)
    output.mkdir(parents=True, exist_ok=True)
    for source_file in (archive, signature):
        (output / source_file.name).write_bytes(source_file.read_bytes())
    lines = [
        f"nmap_version={version}",
        f"source_file={name}",
        f"source_url={source_url}",
        f"source_sha256={actual}",
        f"source_size_bytes={archive.stat().st_size}",
        f"signature_file={signature.name}",
        f"signature_sha256={file_sha256(signature)}",
        f"signing_key_fingerprint={PROJECT_KEY}",
        "signature_verified=true",
        "license=Nmap Public Source License (NPSL)",
        f"license_url={NPSL_URL}",
        f"image={image_ref}",
        f"image_architectures={','.join(sorted(architectures))}",
        "image_source_sha256_matches=true",
    ]
    (output / "nmap-source-manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    try:
        if argv == ["latest"]:
            with open_https(DOWNLOAD_PAGE, 30) as response:
                page = response.read(2_000_001)
            if len(page) > 2_000_000:
                raise ValueError("Nmap download page exceeds size limit")
            print(latest_release(page.decode("utf-8")))
            return 0
        if len(argv) in (3, 4) and argv[0] == "fetch":
            fetch(argv[1], Path(argv[2]), argv[3] if len(argv) == 4 else None)
            return 0
        if len(argv) >= 7 and argv[0] == "release-assets":
            release_assets(argv[1], Path(argv[2]), Path(argv[3]), argv[4], argv[5], [Path(a) for a in argv[6:]])
            return 0
    except (OSError, UnicodeError, ValueError, urllib.error.URLError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "Usage: nmap-release.py latest | fetch VERSION OUTPUT_DIR [SOURCE_SHA256] | release-assets ...", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
