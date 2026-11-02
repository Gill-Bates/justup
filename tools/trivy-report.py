#!/usr/bin/env python3
#
# tools/trivy-report.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Evaluate a Trivy JSON image report; one place for the release gate, the summary and the rescan.

A report counts only when it proves that something was scanned: a JSON object with a non-empty
``Results`` list that includes an OS-package result (an image without one was not recognized) and,
with ``--expect-ref``, whose artifact is the image that was meant to be scanned. "0 findings" is
printed only after this succeeds. A scanner failure (no or invalid report, exit 1) and real
findings (valid report, counts > 0, exit 0) are reported separately; the Trivy step itself owns the
blocking exit code for findings.

  status  <report> [--expect-ref REF]            one line, e.g. "✅ No HIGH/CRITICAL vulnerabilities found"
  summary <report> <label> [--expect-ref REF]    Markdown for $GITHUB_STEP_SUMMARY

Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATED = ("CRITICAL", "HIGH")
MAX_ROWS = 20


class ReportError(Exception):
    """The report is missing, malformed or does not describe the expected scan."""


def load(path: Path, expect_ref: str | None = None) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReportError(f"{path}: cannot read report ({error})") from error
    if not isinstance(report, dict):
        raise ReportError(f"{path}: report is not a JSON object")
    results = report.get("Results")
    if not isinstance(results, list) or not results:
        raise ReportError(f"{path}: no scan results; the scan did not process any target")
    if not all(isinstance(result, dict) for result in results):
        raise ReportError(f"{path}: malformed Results entry")
    if not any(result.get("Class") == "os-pkgs" for result in results):
        raise ReportError(f"{path}: no OS package result; the image was not recognized")
    for result in results:
        vulnerabilities = result.get("Vulnerabilities", [])
        if not isinstance(vulnerabilities, list) or not all(isinstance(v, dict) for v in vulnerabilities):
            raise ReportError(f"{path}: malformed Vulnerabilities in target {result.get('Target')!r}")
    if expect_ref:
        metadata = report.get("Metadata") or {}
        names = [report.get("ArtifactName"), *(metadata.get("RepoTags") or []), *(metadata.get("RepoDigests") or [])]
        if not any(isinstance(n, str) and expect_ref in n for n in names):
            raise ReportError(f"{path}: report describes {report.get('ArtifactName')!r}, expected {expect_ref!r}")
    return report


def findings(report: dict) -> list[dict]:
    return [v for r in report["Results"] for v in r.get("Vulnerabilities", []) if v.get("Severity") in GATED]


def status_line(report: dict) -> str:
    rows = findings(report)
    critical = sum(v["Severity"] == "CRITICAL" for v in rows)
    high = len(rows) - critical
    if not rows:
        return "✅ No HIGH/CRITICAL vulnerabilities found"
    return f"⚠️ {critical} CRITICAL, {high} HIGH vulnerabilities"


def summary(report: dict, label: str) -> str:
    lines = [f"### Trivy Scan Results ({label})"]
    rows = findings(report)
    if not rows:
        return "\n".join([*lines, status_line(report)])
    lines += ["```"]
    lines += [
        f"{v['Severity']}\t{v.get('VulnerabilityID')}\t{v.get('PkgName')}\t{v.get('InstalledVersion')}"
        for v in rows[:MAX_ROWS]
    ]
    lines += ["```"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = argv[1:]
    expect_ref = None
    if "--expect-ref" in args:
        position = args.index("--expect-ref")
        expect_ref = args[position + 1] if position + 1 < len(args) else None
        if expect_ref is None:
            print(__doc__, file=sys.stderr)
            return 2
        del args[position : position + 2]
    try:
        if len(args) == 2 and args[0] == "status":
            print(status_line(load(Path(args[1]), expect_ref)))
            return 0
        if len(args) == 3 and args[0] == "summary":
            print(summary(load(Path(args[1]), expect_ref), args[2]))
            return 0
    except ReportError as error:
        print(f"ERROR: Trivy scanner failure, not a clean result: {error}", file=sys.stderr)
        return 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
