#!/usr/bin/env python3
#
# tests/test_release_tools.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release helpers: verify-manifest.py, toolchain-parity.py and trivy-report.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


manifest_tool = load("verify-manifest")
parity = load("toolchain-parity")
trivy = load("trivy-report")

AMD64 = "sha256:" + "a" * 64
ARM64 = "sha256:" + "b" * 64


def index(*entries):
    return {"manifests": [{"digest": d, "platform": p} for d, p in entries]}


def platform(arch, variant=None, os_name="linux"):
    return {"os": os_name, "architecture": arch, **({"variant": variant} if variant else {})}


GOOD = index(
    (AMD64, platform("amd64")),
    (ARM64, platform("arm64")),
    ("sha256:" + "c" * 64, platform("unknown", os_name="unknown")),
)
PUSHED = {"linux/amd64": AMD64, "linux/arm64": ARM64}


def test_manifest_matches_pushed_digests_and_ignores_attestations():
    assert manifest_tool.verify(GOOD, PUSHED) == PUSHED


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"mediaType": "x"}, "not a manifest index"),
        (index((AMD64, platform("amd64"))), "expected"),
        (index((AMD64, platform("amd64")), (ARM64, platform("arm64")), (ARM64, platform("arm64"))), "duplicate"),
        (index((AMD64, platform("amd64")), ("sha256:" + "d" * 64, platform("arm64"))), "differ"),
    ],
)
def test_manifest_mismatch_fails(bad, message):
    with pytest.raises(manifest_tool.ManifestError, match=message):
        manifest_tool.verify(bad, PUSHED)


def test_manifest_cli_verify_and_children(tmp_path, capsys):
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests" / "amd64").write_text(AMD64)
    (tmp_path / "digests" / "arm64").write_text(ARM64)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(GOOD))
    assert manifest_tool.main(["x", "verify", str(manifest), str(tmp_path / "digests")]) == 0
    assert manifest_tool.main(["x", "children", str(manifest), "linux/amd64", "linux/arm64"]) == 0
    assert f"linux/arm64 {ARM64}" in capsys.readouterr().out
    # A missing child is a failure, not a shorter scan.
    assert manifest_tool.main(["x", "children", str(manifest), "linux/amd64", "linux/arm64", "linux/s390x"]) == 1
    assert manifest_tool.main(["x", "children"]) == 2


FIELDS = {
    "python": "3.13.1",
    "app_version": "1.0.1",
    "java_version": "25",
    "libsignal_client": "0.1",
    "signal_cli": "0.14",
    "fastapi": "1",
}


def test_parity_passes_identical_complete_manifests():
    assert parity.problems({"amd64": dict(FIELDS), "arm64": dict(FIELDS)}, list(parity.REQUIRED_FIELDS)) == []


def test_parity_flags_difference_and_single_architecture():
    other = {**FIELDS, "java_version": "24"}
    assert any("java_version" in p for p in parity.problems({"amd64": FIELDS, "arm64": other}, []))
    assert any("at least two" in p for p in parity.problems({"amd64": FIELDS}, []))


@pytest.mark.parametrize("field", ["java_version", "libsignal_client", "signal_cli", "python"])
def test_parity_requires_fields_even_when_missing_everywhere(field):
    gap = {k: v for k, v in FIELDS.items() if k != field}
    found = parity.problems({"amd64": gap, "arm64": dict(gap)}, list(parity.REQUIRED_FIELDS))
    assert any(f"'{field}' is missing" in p for p in found)
    empty = {**FIELDS, field: ""}
    assert parity.problems({"amd64": empty, "arm64": dict(empty)}, [field])


def test_parity_requires_every_direct_dependency(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    deps = '["FastAPI>=1", "pydantic_settings[extra]>=2; python_version>\'3\'"]'
    pyproject.write_text(f'[project]\nname="x"\nversion="1"\ndependencies={deps}\n')
    assert parity.direct_dependencies(pyproject) == ["fastapi", "pydantic-settings"]
    for arch in ("amd64", "arm64"):
        (tmp_path / "t").mkdir(exist_ok=True)
        (tmp_path / "t" / f"{arch}.txt").write_text("\n".join(f"{k}={v}" for k, v in FIELDS.items()))
    assert parity.main(["x", str(tmp_path / "t"), "--pyproject", str(pyproject)]) == 1
    assert "pydantic-settings" in capsys.readouterr().err
    assert parity.main(["x", str(tmp_path / "t")]) == 0


def report(vulns=(), classes=("os-pkgs",), name="justup:test-amd64", **extra):
    results = [{"Target": f"t{i}", "Class": c} for i, c in enumerate(classes)]
    if vulns and results:
        results[0]["Vulnerabilities"] = [
            {"Severity": s, "VulnerabilityID": f"CVE-{i}", "PkgName": "p", "InstalledVersion": "1"}
            for i, s in enumerate(vulns)
        ]
    return {"ArtifactName": name, "Results": results, **extra}


def write(tmp_path, content) -> Path:
    path = tmp_path / "r.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def test_clean_valid_report_reports_zero_findings(tmp_path):
    assert trivy.status_line(trivy.load(write(tmp_path, report()), "justup:test-amd64")).startswith("✅")


def test_findings_are_counted_separately_from_scanner_errors(tmp_path):
    loaded = trivy.load(write(tmp_path, report(["CRITICAL", "HIGH", "HIGH", "LOW"])))
    assert trivy.status_line(loaded) == "⚠️ 1 CRITICAL, 2 HIGH vulnerabilities"
    assert "CVE-0" in trivy.summary(loaded, "amd64")


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        {"ArtifactName": "x"},
        {"ArtifactName": "x", "Results": []},
        {"ArtifactName": "x", "Results": [{"Class": "lang-pkgs"}]},
        {"ArtifactName": "x", "Results": [{"Class": "os-pkgs", "Vulnerabilities": "oops"}]},
        {"ArtifactName": "x", "Results": ["str"]},
    ],
)
def test_invalid_or_empty_report_is_never_zero_findings(tmp_path, content):
    with pytest.raises(trivy.ReportError):
        trivy.load(write(tmp_path, content))


def test_missing_report_and_wrong_target_fail(tmp_path):
    with pytest.raises(trivy.ReportError):
        trivy.load(tmp_path / "absent.json")
    with pytest.raises(trivy.ReportError, match="expected"):
        trivy.load(write(tmp_path, report(name="other:image")), "justup:test-amd64")
    tagged = report(name="abc", Metadata={"RepoTags": ["justup:test-amd64"]})
    assert trivy.load(write(tmp_path, tagged), "justup:test-amd64")


def test_trivy_cli_exit_codes(tmp_path, capsys):
    good = write(tmp_path, report(["HIGH"]))
    assert trivy.main(["x", "status", str(good)]) == 0
    assert trivy.main(["x", "summary", str(good), "amd64", "--expect-ref", "justup:test-amd64"]) == 0
    assert "HIGH" in capsys.readouterr().out
    assert trivy.main(["x", "status", str(tmp_path / "absent.json")]) == 1
    assert "scanner failure" in capsys.readouterr().err
    assert trivy.main(["x", "status"]) == 2
