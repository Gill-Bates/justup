#!/usr/bin/env python3
#
# tests/test_release_tools.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Release helpers: verify-manifest.py, toolchain-parity.py and trivy-report.py."""

from __future__ import annotations

import importlib.util
import json
import re
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


def test_manifest_rejects_two_entries_for_one_architecture_even_with_different_variants():
    other = "sha256:" + "e" * 64
    # The last entry matches the pushed digest, so a collapsing os/architecture map would pass.
    bad = index((other, platform("arm64", "v7")), (ARM64, platform("arm64", "v8")), (AMD64, platform("amd64")))
    with pytest.raises(manifest_tool.ManifestError, match=r"more than one entry for: linux/arm64"):
        manifest_tool.verify(bad, PUSHED)
    with pytest.raises(manifest_tool.ManifestError, match="more than one entry"):
        manifest_tool.platform_digests(bad)


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
    "nmap": "7.993",
    "nmap_source_sha256": "a" * 64,
    "fastapi": "1",
}


def test_parity_passes_identical_complete_manifests():
    assert parity.problems({"amd64": dict(FIELDS), "arm64": dict(FIELDS)}, list(parity.REQUIRED_FIELDS)) == []


def test_parity_flags_difference_and_single_architecture():
    other = {**FIELDS, "java_version": "24"}
    assert any("java_version" in p for p in parity.problems({"amd64": FIELDS, "arm64": other}, []))
    assert any("at least two" in p for p in parity.problems({"amd64": FIELDS}, []))


@pytest.mark.parametrize(
    "field", ["java_version", "libsignal_client", "signal_cli", "nmap", "nmap_source_sha256", "python"]
)
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


# --- nmap-release.py: stable-release resolution and signature policy ---------------------

nmap_release = load("nmap-release")
DIST = nmap_release.DIST
STABLE_PAGE = (
    "<b>Latest stable Nmap release tarball: "
    f'<a href="{DIST}/nmap-7.993.tar.bz2">nmap-7.993.tar.bz2</a></b>'
    f'<a href="{DIST}/nmap-7.992.tar.bz2">older</a>'
    f'<b>Latest development: <a href="{DIST}/nmap-7.999BETA1.tar.bz2">beta</a></b>'
)


def test_nmap_latest_takes_only_the_stable_tarball():
    assert nmap_release.latest_release(STABLE_PAGE) == "7.993"


@pytest.mark.parametrize(
    "page",
    [
        "<p>nothing</p>",
        STABLE_PAGE + STABLE_PAGE,
        STABLE_PAGE.replace("nmap-7.993.tar.bz2", "nmap-7.993BETA1.tar.bz2"),
        STABLE_PAGE.replace(DIST, "https://evil.example/dist"),
    ],
)
def test_nmap_latest_requires_exactly_one_official_stable_source(page):
    with pytest.raises(ValueError, match="exactly one"):
        nmap_release.latest_release(page)


class FakeGpg:
    def __init__(self, status: str, returncode: int = 0):
        self.status, self.returncode = status, returncode

    def __call__(self, command, **_kwargs):
        if "--import" in command:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": self.returncode, "stdout": self.status, "stderr": ""})()


def validsig(signing: str, primary: str) -> str:
    return f"[GNUPG:] VALIDSIG {signing} 2026-01-01 1 0 4 0 1 10 00 {primary}\n"


def run_verify(monkeypatch, tmp_path, status: str, returncode: int = 0) -> None:
    monkeypatch.setattr(nmap_release.subprocess, "run", FakeGpg(status, returncode))
    nmap_release.verify_signature(tmp_path / "a.tar.bz2", tmp_path / "a.asc", tmp_path / "keys", tmp_path / "gnupg")


def test_nmap_signature_accepts_the_pinned_key_also_for_a_subkey(monkeypatch, tmp_path):
    run_verify(monkeypatch, tmp_path, validsig("A" * 40, nmap_release.PROJECT_KEY))


@pytest.mark.parametrize(
    ("status", "returncode"),
    [
        (validsig("B" * 40, "B" * 40), 0),
        (validsig(nmap_release.PROJECT_KEY, nmap_release.PROJECT_KEY) * 2, 0),
        (validsig(nmap_release.PROJECT_KEY, nmap_release.PROJECT_KEY), 1),
        ("[GNUPG:] EXPKEYSIG X\n" + validsig(nmap_release.PROJECT_KEY, nmap_release.PROJECT_KEY), 0),
        ("[GNUPG:] REVKEYSIG X\n" + validsig(nmap_release.PROJECT_KEY, nmap_release.PROJECT_KEY), 0),
        ("[GNUPG:] BADSIG X\n", 1),
        ("", 0),
    ],
)
def test_nmap_signature_rejects_untrusted_bad_or_ambiguous_results(monkeypatch, tmp_path, status, returncode):
    with pytest.raises(ValueError, match="invalid or was made by an untrusted key"):
        run_verify(monkeypatch, tmp_path, status, returncode)


def stub_downloads(monkeypatch, content: bytes = b"archive"):
    import hashlib

    def fake_download(_url, path, **_kwargs):
        path.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(nmap_release, "download", fake_download)
    return hashlib.sha256(content).hexdigest()


def test_nmap_fetch_records_provenance_only_after_signature_and_hash_match(monkeypatch, tmp_path):
    digest = stub_downloads(monkeypatch)
    monkeypatch.setattr(nmap_release, "verify_signature", lambda *_a: None)
    nmap_release.fetch("7.993", tmp_path / "out", digest)
    lines = (tmp_path / "out" / "provenance.txt").read_text().splitlines()
    assert lines == [
        "nmap_version=7.993",
        f"source_url={DIST}/nmap-7.993.tar.bz2",
        f"source_sha256={digest}",
        f"signing_key_fingerprint={nmap_release.PROJECT_KEY}",
    ]


def test_nmap_fetch_rejects_a_source_hash_that_differs_from_the_resolved_one(monkeypatch, tmp_path):
    stub_downloads(monkeypatch)
    monkeypatch.setattr(nmap_release, "verify_signature", lambda *_a: None)
    with pytest.raises(ValueError, match="differs from the resolved"):
        nmap_release.fetch("7.993", tmp_path / "out", "0" * 64)
    assert not (tmp_path / "out" / "provenance.txt").exists()


def test_nmap_fetch_aborts_on_a_failed_signature_without_provenance(monkeypatch, tmp_path):
    stub_downloads(monkeypatch)

    def reject(*_args):
        raise ValueError("Nmap signature is invalid or was made by an untrusted key: x")

    monkeypatch.setattr(nmap_release, "verify_signature", reject)
    with pytest.raises(ValueError, match="untrusted key"):
        nmap_release.fetch("7.993", tmp_path / "out")
    assert not (tmp_path / "out" / "provenance.txt").exists()


@pytest.mark.parametrize(("version", "digest"), [("7.993;rm", None), ("", None), ("7.993", "XYZ")])
def test_nmap_fetch_rejects_malformed_inputs_before_any_download(tmp_path, version, digest):
    with pytest.raises(ValueError):
        nmap_release.fetch(version, tmp_path / "out", digest)
    assert not (tmp_path / "out").exists()


def test_nmap_download_refuses_non_https_urls(tmp_path):
    with pytest.raises(ValueError, match="non-HTTPS"):
        nmap_release.download("http://nmap.org/dist/x", tmp_path / "x", limit=10)


def test_nmap_cli_reports_usage_and_errors(capsys):
    assert nmap_release.main(["bogus"]) == 2
    assert nmap_release.main(["fetch", "bad version", "/nonexistent-out"]) == 1
    assert "invalid Nmap version" in capsys.readouterr().err


def test_dockerfile_nmap_stage_builds_the_pinned_source_without_fixed_version_package_or_default():
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    stage = text[text.index("AS nmap-builder") : text.index("AS signal-cli-src")]
    for arg in ("NMAP_VERSION", "NMAP_SOURCE_SHA256"):
        assert f"\nARG {arg}\n" in stage  # mandatory, no default value
    assert 'fetch "$version" /tmp/nmap "$source_sha256"' in stage  # hash is enforced in the builder
    assert not re.search(r"nmap[-= ]\d+\.\d+", stage.replace("nmap-${version}", ""))
    assert not re.search(r"apt-get install[^\n]*\bnmap\b", text)
    assert "rpm2cpio" not in stage and "NMAP_RPM" not in stage
    assert "nmap-release.py latest" not in stage.split("RUN <<'EOF'", 1)[1]  # no resolution cached in a RUN


def test_dockerfile_runtime_copies_only_explicit_nmap_paths():
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY --from=nmap-builder (\S+) (\S+)$", text, re.MULTILINE)
    assert copies == [
        ("/out/usr/bin/nmap", "/usr/bin/nmap"),
        ("/out/usr/share/nmap", "/usr/share/nmap"),
        ("/out/usr/share/doc/nmap", "/usr/share/doc/nmap"),
    ]
    runtime = text[text.index("AS runtime") :]
    assert not re.search(r"\b(build-essential|gcc|make|configure)\b", runtime)


def nmap_workflow_problems(workflow: str, builder_fields: set[str]) -> list[str]:
    """Contract between docker-build.yml, nmap-release.py and the fields the Dockerfile builder writes."""
    problems = []
    if re.search(r"nmap_rpm|NMAP_RPM|artifact_sha256|nmap-release\.py fetch[^\n]*\$nmap_rpm", workflow):
        problems.append("workflow still uses the RPM contract (nmap_rpm / NMAP_RPM / artifact_sha256)")
    if 'tools/nmap-release.py latest)"' not in workflow:
        problems.append("meta does not resolve the version with `nmap-release.py latest`")
    if not re.search(r'nmap-release\.py fetch "\$nmap_version" "\$verify_dir"', workflow):
        problems.append("meta does not call `fetch VERSION DIR` with the new signature")
    for arg, source in (("NMAP_VERSION", "nmap_version"), ("NMAP_SOURCE_SHA256", "nmap_source_sha256")):
        if workflow.count(f'--build-arg "{arg}=${{{{ needs.meta.outputs.{source} }}}}"') != 1:
            problems.append(f"build-arg {arg} is not passed (once, from meta) to the matrix build")
    gate = workflow[workflow.index("Verify Nmap TCP and UDP in the runtime image") :]
    gate = gate[: gate.index("- name: Upload Nmap provenance")]
    problems.extend(
        f"provenance gate does not check '{field}' written by the builder"
        for field in sorted(builder_fields)
        if f"{field}=" not in gate
    )
    if not re.search(r"--entrypoint sha256sum [^\n]*/usr/bin/nmap", gate) or "binary_sha256=${shipped}" not in gate:
        problems.append("gate does not compare the shipped /usr/bin/nmap hash with binary_sha256")
    if "- binary " not in gate or "verify-nmap-worker.py" not in workflow:
        problems.append("gate lacks the ELF/library check or the -sV worker scan")
    if "build_method=verified-source" not in gate:
        problems.append("gate does not require build_method=verified-source on every architecture")
    return problems


def dockerfile_builder_fields() -> set[str]:
    text = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    stage = text[text.index("AS nmap-builder") : text.index("AS signal-cli-src")]
    block = stage[stage.index("cat /tmp/nmap/provenance.txt") : stage.index("> /out/usr/share/doc/nmap/PROVENANCE.txt")]
    # provenance.txt (nmap_version, source_*, signing_key_fingerprint) is copied; the builder appends the rest.
    return {"nmap_version", "source_sha256", "signing_key_fingerprint"} | set(
        re.findall(r"(?:'|\\n)([a-z][a-z0-9_]*)=", block)
    )


def test_builder_writes_the_provenance_fields_the_release_contract_names():
    assert dockerfile_builder_fields() >= {
        "architecture",
        "build_method",
        "binary_sha256",
        "runtime_dependencies",
        "nmap_version",
        "source_sha256",
        "signing_key_fingerprint",
    }


def test_release_workflow_matches_the_nmap_release_tool_and_the_builder_contract():
    workflow = (ROOT / ".github" / "workflows" / "docker-build.yml").read_text(encoding="utf-8")
    assert nmap_workflow_problems(workflow, dockerfile_builder_fields()) == []


def test_contract_check_rejects_the_rpm_era_workflow():
    workflow = (ROOT / ".github" / "workflows" / "docker-build.yml").read_text(encoding="utf-8")
    old = workflow.replace('--build-arg "NMAP_SOURCE_SHA256=', '--build-arg "NMAP_RPM=').replace(
        "binary_sha256=${shipped}", "artifact_sha256"
    )
    problems = nmap_workflow_problems(old, dockerfile_builder_fields())
    assert any("RPM contract" in p for p in problems)
    assert any("NMAP_SOURCE_SHA256" in p for p in problems)
    assert any("binary_sha256" in p for p in problems)


@pytest.mark.skipif(sys.platform != "linux", reason="needs a Linux ELF binary")
def test_nmap_worker_check_reads_the_dynamic_section_of_an_elf():
    check = load("verify-nmap-worker")
    tags = check.dynamic_tags(Path(sys.executable).resolve().read_bytes())
    assert 1 in tags  # DT_NEEDED
    assert 5 in tags  # DT_STRTAB
