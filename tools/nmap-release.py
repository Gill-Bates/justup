#!/usr/bin/env python3
#
# tools/nmap-release.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Resolve Nmap's official stable release and verify its build artifacts."""

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
RPM_RE = re.compile(r"^nmap-([0-9]+(?:\.[0-9]+)+)-[0-9]+\.x86_64\.rpm$")


class StableLinks(HTMLParser):
    """Collect the stable source link and published x86-64 RPM link."""

    def __init__(self) -> None:
        super().__init__()
        self.bold_depth = 0
        self.bold_text: list[str] = []
        self.bold_links: list[str] = []
        self.source_links: list[str] = []
        self.rpm_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "b":
            if self.bold_depth == 0:
                self.bold_text = []
                self.bold_links = []
            self.bold_depth += 1
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if self.bold_depth:
                self.bold_links.append(href)
            if href.startswith(f"{DIST}/") and RPM_RE.fullmatch(href.rsplit("/", 1)[-1]):
                self.rpm_links.append(href)

    def handle_data(self, data: str) -> None:
        if self.bold_depth:
            self.bold_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "b" or not self.bold_depth:
            return
        self.bold_depth -= 1
        if self.bold_depth == 0 and "Latest stable Nmap release tarball:" in " ".join("".join(self.bold_text).split()):
            self.source_links.extend(self.bold_links)


def latest_release(page: str) -> tuple[str, str]:
    parser = StableLinks()
    parser.feed(page)
    sources = []
    for url in parser.source_links:
        match = re.fullmatch(rf"{re.escape(DIST)}/nmap-([0-9]+(?:\.[0-9]+)+)\.tar\.bz2", url)
        if match:
            sources.append(match.group(1))
    if len(sources) != 1:
        raise ValueError("expected exactly one official stable Nmap source tarball")
    version = sources[0]
    rpms = [
        url.rsplit("/", 1)[-1]
        for url in parser.rpm_links
        if RPM_RE.fullmatch(url.rsplit("/", 1)[-1]).group(1) == version
    ]
    if len(rpms) > 1:
        raise ValueError("expected at most one official x86-64 RPM for the stable release")
    return version, rpms[0] if rpms else "-"


def download(url: str, path: Path, *, limit: int) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "justUp-Nmap-build/1"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, path.open("wb") as target:
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
    )
    # VALIDSIG ends with the primary key fingerprint, also when a subkey signed.
    valid_signers = [line.split()[-1] for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    bad_status = any(line.startswith(BAD_STATUS) for line in result.stdout.splitlines())
    if result.returncode or bad_status or valid_signers != [PROJECT_KEY]:
        raise ValueError(f"Nmap signature is invalid or was made by an untrusted key: {artifact.name}")


def fetch(version: str, rpm: str, output: Path) -> None:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("invalid Nmap version")
    if rpm != "-" and (not RPM_RE.fullmatch(rpm) or RPM_RE.fullmatch(rpm).group(1) != version):
        raise ValueError("RPM filename does not match the selected stable release")
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
    lines = [
        f"nmap_version={version}",
        f"signing_key_fingerprint={PROJECT_KEY}",
        f"source_url={source_url}",
        f"source_sha256={source_hash}",
    ]
    if rpm != "-":
        rpm_url = f"{DIST}/{rpm}"
        rpm_hash = download(rpm_url, output / rpm, limit=100_000_000)
        rpm_sig = output / f"{rpm}.asc"
        download(f"{DIST}/sigs/{rpm}.asc", rpm_sig, limit=100_000)
        verify_signature(output / rpm, rpm_sig, keyfile, output / "gnupg")
        lines.extend((f"rpm_url={rpm_url}", f"rpm_sha256={rpm_hash}"))
    (output / "provenance.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    try:
        if argv == ["latest"]:
            request = urllib.request.Request(DOWNLOAD_PAGE, headers={"User-Agent": "justUp-Nmap-build/1"})
            with urllib.request.urlopen(request, timeout=30) as response:
                page = response.read(2_000_001)
            if len(page) > 2_000_000:
                raise ValueError("Nmap download page exceeds size limit")
            print(*latest_release(page.decode("utf-8")))
            return 0
        if len(argv) == 4 and argv[0] == "fetch":
            fetch(argv[1], argv[2], Path(argv[3]))
            return 0
    except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Usage: nmap-release.py latest | fetch VERSION RPM_FILENAME|- OUTPUT_DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
