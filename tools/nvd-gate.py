#!/usr/bin/env python3
#
# tools/nvd-gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""SBOM and NVD gate for the software compiled into the Nmap binary (Trivy cannot see it).

Trivy matches packages from a package database or a language lockfile against distribution and
ecosystem advisories. Nmap and the libraries compiled into it (libpcap, libdnet, liblinear) have
no dpkg entry and no purl type those advisories use, so Trivy reports nothing for them. This tool
closes that gap for the components that have a CPE in the NVD dictionary:

  sbom   <PROVENANCE.txt> <out.cdx.json>    CycloneDX 1.5 SBOM from the image provenance file
                                            (exact Nmap version, bundled library versions), with an
                                            explicit purl and, where NVD has one, a CPE per component
  check  <sbom.json> [--ignore FILE] [--report FILE]
                                            ask the NVD CVE API 2.0 (virtualMatchString, ranges are
                                            evaluated by NVD) for every component that carries a CPE
                                            and block on CRITICAL CVEs (CVSS v4.0, v3.1 or v3.0 base
                                            score >= 9.0)

Exit codes: 0 clean, 1 blocked by at least one CRITICAL CVE, 2 usage, 3 the check could not be
performed (unreachable or malformed NVD answer, invalid input). Never a silent pass: an NVD failure
is exit 3. Components without an NVD CPE (libdnet, liblinear at the time of writing) are listed as
NOT COVERED in the output and the report; they are not scanned by any tool here.

Accepted CVEs live in the versioned ``.nvdignore`` (one ``CVE-YYYY-NNNN reason`` per line, the
reason is mandatory). ``NVD_API_KEY`` (optional) raises the NVD rate limit and is sent as a header,
never printed.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CRITICAL_SCORE = 9.0
PAGE_SIZE = 2000
MAX_PAGES = 20
TIMEOUT_SECONDS = 60
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
RETRY_DELAYS = (10, 30, 60)
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)+")
CPE_RE = re.compile(r"cpe:2\.3:a:[a-z0-9_]+:[a-z0-9_]+:[0-9]+(?:\.[0-9]+)+(?::\*){7}")
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}")
METRIC_KEYS = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30")
MIN_REASON_LENGTH = 10

# component -> (CPE vendor, CPE product) from the NVD dictionary, or None when NVD has no entry.
# Checked against the NVD CPE dictionary on 2026-11-05: nmap:nmap and tcpdump:libpcap exist;
# a keyword search for libdnet and a product search for liblinear return nothing.
CPE_NAMES: dict[str, tuple[str, str] | None] = {
    "nmap": ("nmap", "nmap"),
    "libpcap": ("tcpdump", "libpcap"),
    "libdnet": None,
    "liblinear": None,
}
NOT_COVERED_REASON = "no CPE in the NVD dictionary; not scanned by any tool here"


class GateError(Exception):
    """The check could not be performed (exit 3)."""


@dataclass(frozen=True)
class Finding:
    cve: str
    score: float
    component: str
    version: str
    ignored_reason: str | None = None


def parse_provenance(path: Path) -> dict[str, str]:
    """Read the Nmap version and the bundled library versions; anything missing or odd is an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise GateError(f"{path}: cannot read provenance ({error})") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("nmap_version", "bundled_libraries"):
            if key in fields:
                raise GateError(f"{path.name}: duplicate field {key}")
            fields[key] = value
    versions: dict[str, str] = {"nmap": fields.get("nmap_version", "")}
    for entry in fields.get("bundled_libraries", "").split(","):
        name, separator, version = entry.partition("=")
        if separator:
            if name in versions:
                raise GateError(f"{path.name}: duplicate bundled library {name}")
            versions[name] = version
    for name in CPE_NAMES:
        if not VERSION_RE.fullmatch(versions.get(name, "")):
            raise GateError(f"{path.name}: no valid version for {name} (got {versions.get(name)!r})")
    unknown = sorted(set(versions) - set(CPE_NAMES))
    if unknown:
        # A new bundled library must get an explicit CPE decision instead of being skipped.
        raise GateError(f"{path.name}: bundled libraries without a CPE decision: {', '.join(unknown)}")
    return versions


def build_sbom(versions: dict[str, str]) -> dict:
    components = []
    for name, version in versions.items():
        component: dict = {
            "type": "application" if name == "nmap" else "library",
            "bom-ref": f"pkg:generic/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:generic/{name}@{version}",
        }
        names = CPE_NAMES[name]
        if names:
            component["cpe"] = f"cpe:2.3:a:{names[0]}:{names[1]}:{version}:*:*:*:*:*:*:*"
        else:
            component["properties"] = [{"name": "justup:nvd-coverage", "value": f"none ({NOT_COVERED_REASON})"}]
        components.append(component)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "application", "name": "justup-nmap-composition"}},
        "components": components,
    }


def load_sbom(path: Path) -> list[dict]:
    try:
        sbom = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError(f"{path}: cannot read SBOM ({error})") from error
    components = sbom.get("components") if isinstance(sbom, dict) else None
    if (
        not isinstance(sbom, dict)
        or sbom.get("bomFormat") != "CycloneDX"
        or not isinstance(components, list)
        or not components
    ):
        raise GateError(f"{path}: not a CycloneDX SBOM with components")
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("name"), str):
            raise GateError(f"{path}: malformed component entry")
        cpe = component.get("cpe")
        if cpe is not None and not (isinstance(cpe, str) and CPE_RE.fullmatch(cpe)):
            raise GateError(f"{path}: component {component['name']!r} has an unsupported CPE {cpe!r}")
    if not any(c["name"] == "nmap" and c.get("cpe") for c in components):
        raise GateError(f"{path}: the SBOM must carry Nmap with a CPE; nothing would be checked")
    return components


def load_ignores(path: Path | None) -> dict[str, str]:
    """``CVE-ID reason`` lines; an entry without a reason is rejected so every acceptance is justified."""
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise GateError(f"{path}: cannot read ignore file ({error})") from error
    ignores: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cve, _, reason = line.partition(" ")
        reason = reason.strip()
        if not CVE_RE.fullmatch(cve) or len(reason) < MIN_REASON_LENGTH:
            raise GateError(
                f"{path.name}:{number}: expected 'CVE-YYYY-NNNN <reason of at least {MIN_REASON_LENGTH} chars>'"
            )
        ignores[cve] = reason
    return ignores


def critical_score(cve: dict) -> float | None:
    """Highest CVSS v4.0/v3.1/v3.0 base score of any source; None when the CVE has no such score."""
    scores = []
    metrics = cve.get("metrics")
    for key in METRIC_KEYS:
        for metric in (metrics.get(key) or []) if isinstance(metrics, dict) else []:
            score = (metric.get("cvssData") or {}).get("baseScore") if isinstance(metric, dict) else None
            if isinstance(score, int | float) and not isinstance(score, bool):
                scores.append(float(score))
    return max(scores) if scores else None


def http_fetch(url: str) -> dict:
    """GET one NVD page with bounded retries; the API key travels only in the header."""
    headers = {"User-Agent": "justup-release-gate", "Accept": "application/json"}
    api_key = os.environ.get("NVD_API_KEY", "").strip()
    if api_key:
        headers["apiKey"] = api_key
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            request = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https endpoint
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise GateError(f"NVD answered HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise GateError(f"NVD request failed ({type(error).__name__})") from error
    raise GateError("NVD request failed")  # pragma: no cover - the loop always returns or raises


def query_cves(cpe: str, fetch: Callable[[str], dict], pause: Callable[[], None]) -> list[dict]:
    """All non-rejected CVEs NVD attributes to ``cpe``; an inconsistent page set is an error."""
    cves: list[dict] = []
    start, total = 0, None
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode({"virtualMatchString": cpe, "resultsPerPage": PAGE_SIZE, "startIndex": start})
        page = fetch(f"{NVD_API}?{query}&noRejected")
        vulnerabilities = page.get("vulnerabilities") if isinstance(page, dict) else None
        page_total = page.get("totalResults") if isinstance(page, dict) else None
        if not isinstance(vulnerabilities, list) or not isinstance(page_total, int) or isinstance(page_total, bool):
            raise GateError(f"NVD answer for {cpe} is malformed")
        if total is not None and total != page_total:
            raise GateError(f"NVD result count for {cpe} changed while paging")
        total = page_total
        for item in vulnerabilities:
            cve = item.get("cve") if isinstance(item, dict) else None
            if not isinstance(cve, dict) or not CVE_RE.fullmatch(str(cve.get("id", ""))):
                raise GateError(f"NVD answer for {cpe} carries a malformed CVE entry")
            if cve.get("vulnStatus") != "Rejected":
                cves.append(cve)
        start += len(vulnerabilities)
        if start >= total:
            return cves
        if not vulnerabilities:
            raise GateError(f"NVD returned an empty page before the announced {total} results for {cpe}")
        pause()
    raise GateError(f"NVD result set for {cpe} exceeds {MAX_PAGES} pages")


def check(
    components: list[dict],
    fetch: Callable[[str], dict],
    ignores: dict[str, str],
    pause: Callable[[], None] = lambda: None,
) -> dict:
    """Query every CPE component; returns the report with findings, ignored CVEs and unscored CVEs."""
    findings: list[Finding] = []
    unscored: list[str] = []
    covered: list[str] = []
    not_covered: list[str] = []
    first = True
    for component in components:
        name, version, cpe = component["name"], str(component.get("version", "")), component.get("cpe")
        if not cpe:
            not_covered.append(f"{name} {version}: {NOT_COVERED_REASON}")
            continue
        if not first:
            pause()
        first = False
        covered.append(f"{name} {version}")
        for cve in query_cves(cpe, fetch, pause):
            score = critical_score(cve)
            if score is None:
                unscored.append(f"{cve['id']} ({name} {version})")
            elif score >= CRITICAL_SCORE:
                findings.append(Finding(cve["id"], score, name, version, ignores.get(cve["id"])))
    return {
        "blocking": [f.__dict__ for f in findings if f.ignored_reason is None],
        "accepted": [f.__dict__ for f in findings if f.ignored_reason is not None],
        "unscored": sorted(unscored),
        "covered": covered,
        "not_covered": not_covered,
    }


def render(report: dict) -> str:
    lines = [f"NVD checked: {', '.join(report['covered'])}"]
    lines += [f"NOT COVERED: {item}" for item in report["not_covered"]]
    lines += [
        f"ACCEPTED ({f['ignored_reason']}): {f['cve']} {f['component']} {f['version']}" for f in report["accepted"]
    ]
    if report["unscored"]:
        lines.append(f"WARNING: no CVSS v3/v4 score yet, not gated: {', '.join(report['unscored'])}")
    lines += [f"BLOCKED: {f['cve']} (CVSS {f['score']}) in {f['component']} {f['version']}" for f in report["blocking"]]
    if not report["blocking"]:
        lines.append("No CRITICAL CVE for the covered components")
    return "\n".join(lines)


def main(argv: list[str], fetch: Callable[[str], dict] = http_fetch) -> int:
    args = argv[1:]
    try:
        if len(args) == 3 and args[0] == "sbom":
            sbom = build_sbom(parse_provenance(Path(args[1])))
            Path(args[2]).write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
            return 0
        if len(args) >= 2 and args[0] == "check":
            options = args[2:]
            ignore_path = report_path = None
            while options:
                if len(options) < 2 or options[0] not in ("--ignore", "--report"):
                    print(__doc__, file=sys.stderr)
                    return 2
                if options[0] == "--ignore":
                    ignore_path = Path(options[1])
                else:
                    report_path = Path(options[1])
                options = options[2:]
            delay = 1.0 if os.environ.get("NVD_API_KEY", "").strip() else 7.0
            report = check(load_sbom(Path(args[1])), fetch, load_ignores(ignore_path), pause=lambda: time.sleep(delay))
            if report_path:
                report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(render(report))
            return 1 if report["blocking"] else 0
    except (GateError, OSError) as error:
        print(f"ERROR: NVD gate could not be evaluated, not a clean result: {error}", file=sys.stderr)
        return 3
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
