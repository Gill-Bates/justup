#!/usr/bin/env python3
#
# tests/test_dependency_lock.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Supply-chain contract of the runtime dependencies.

docker/requirements.lock is the complete, hash-pinned, wheel-only runtime set; SSLyze is
the vendored justUp wheel in docker/vendor/wheels/ rather than a PyPI release or a source
archive. pyproject.toml, the lock, the wheel's own metadata and the installers (Dockerfile,
CI) must agree, and a wrong hash or missing wheel must stop an install.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
WHEEL_DIR = ROOT / "docker" / "vendor" / "wheels"
PROVENANCE = ROOT / "docker" / "vendor" / "PROVENANCE.json"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "docker-build.yml"
SSLYZE_BUILD_SCRIPT = ROOT / "docker" / "vendor" / "sslyze" / "build-wheel.sh"


def _load_tool(filename: str):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_").removesuffix(".py"), ROOT / "tools" / filename
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _lock_entries() -> dict[str, tuple[str, list[str]]]:
    """Map canonical name -> (version, sha256 list); rejects anything that is not name==version + hashes."""
    text = (ROOT / "docker" / "requirements.lock").read_text(encoding="utf-8").replace("\\\n", " ")
    entries = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec, *hashes = (part.strip() for part in line.split(" --hash="))
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9A-Za-z.+!_-]+)", spec.strip())
        assert match, f"lock entry is not an exact name==version pin: {spec}"
        assert hashes and all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in hashes), f"bad hashes for {spec}"
        entries[canonicalize_name(match.group(1))] = (match.group(2), [h.removeprefix("sha256:") for h in hashes])
    return entries


def _direct_dependencies() -> list[Requirement]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [Requirement(d) for d in data["project"]["dependencies"]]


def _sslyze_wheel() -> Path:
    wheels = sorted(WHEEL_DIR.glob("*.whl"))
    assert [w.name for w in wheels if w.name.startswith("sslyze-")] == [w.name for w in wheels], (
        "only the SSLyze wheel is vendored"
    )
    assert len(wheels) == 1, f"expected exactly one vendored wheel, found {[w.name for w in wheels]}"
    return wheels[0]


def _wheel_metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        meta_name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return Parser().parsestr(archive.read(meta_name).decode("utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- lock <-> pyproject.toml ------------------------------------------------------------


def test_every_direct_dependency_is_locked():
    entries = _lock_entries()
    for dep in _direct_dependencies():
        assert canonicalize_name(dep.name) in entries, f"{dep.name} missing from docker/requirements.lock"


def test_locked_versions_satisfy_direct_requirements():
    entries = _lock_entries()
    for dep in _direct_dependencies():
        version = entries[canonicalize_name(dep.name)][0]
        assert dep.specifier.contains(version, prereleases=True), f"{dep.name} {version} violates '{dep.specifier}'"


def test_no_direct_dependency_is_a_url_or_vcs_requirement():
    for dep in _direct_dependencies():
        assert dep.url is None, f"{dep.name} is installed from a URL ({dep.url}); vendor a hash-pinned wheel instead"


def test_lock_and_manifests_do_not_reference_github_sslyze_sources():
    for rel in ("pyproject.toml", "docker/requirements.lock", "docker/Dockerfile", ".github/workflows/ci.yml"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"github\.com/nabla-c0d3/sslyze/(archive|tarball|zipball)|git\+https?://", text), rel


def test_lock_has_no_unpinned_or_non_wheel_entries():
    # _lock_entries() already rejects '@ url', ranges and hashless entries.
    assert len(_lock_entries()) > 20


def test_cryptography_is_patched_release():
    assert Version(_lock_entries()["cryptography"][0]) >= Version("50.0.2")


def test_no_vulnerability_ignore_entries():
    lines = (ROOT / ".trivyignore").read_text(encoding="utf-8").splitlines()
    assert [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")] == []


# --- vendored SSLyze wheel ----------------------------------------------------------------


def test_sslyze_pin_matches_wheel_version_in_manifest_and_lock():
    wheel = _sslyze_wheel()
    name, version, _, tags = parse_wheel_filename(wheel.name)
    assert name == "sslyze" and version.local and version.local.startswith("justup.")
    assert next(d for d in _direct_dependencies() if d.name == "sslyze").specifier == f"=={version}"
    assert _lock_entries()["sslyze"][0] == str(version)
    assert {str(t) for t in tags} == {"py3-none-any"}, "a platform-specific wheel needs per-arch handling"


def test_lock_hash_equals_actual_wheel_sha256_and_provenance():
    wheel = _sslyze_wheel()
    actual = _sha256(wheel)
    assert _lock_entries()["sslyze"][1] == [actual]
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["sha256"] == actual
    assert provenance["size_bytes"] == wheel.stat().st_size
    assert provenance["version"] == str(parse_wheel_filename(wheel.name)[1])
    assert re.fullmatch(r"[0-9a-f]{40}", provenance["upstream"]["commit"])
    build_script = (ROOT / "docker" / "vendor" / "sslyze" / "build-wheel.sh").read_text(encoding="utf-8")
    assert f'UPSTREAM_COMMIT="{provenance["upstream"]["commit"]}"' in build_script


def test_wheel_metadata_license_and_cryptography_range():
    meta = _wheel_metadata(_sslyze_wheel())
    assert meta["Name"] == "sslyze" and meta["Version"] == str(parse_wheel_filename(_sslyze_wheel().name)[1])
    assert "LICENSE.txt" in meta.get_all("License-File", [])
    requires = {canonicalize_name(r.name): r for r in map(Requirement, meta.get_all("Requires-Dist", []))}
    crypto = requires["cryptography"].specifier
    assert crypto.contains("50.0.2"), "wheel must accept the patched cryptography"
    assert not crypto.contains("51.0.0"), "upper bound must stay below the untested 51 line"
    entries = _lock_entries()
    for name, req in requires.items():
        assert name in entries, f"{name} (required by the sslyze wheel) missing from the lock"
        assert req.specifier.contains(entries[name][0]), f"locked {name} violates the sslyze wheel's '{req.specifier}'"


def test_wheel_contents_match_record_and_carry_no_junk():
    with zipfile.ZipFile(_sslyze_wheel()) as archive:
        names = archive.namelist()
        record = next(n for n in names if n.endswith(".dist-info/RECORD"))
        for line in archive.read(record).decode("utf-8").splitlines():
            path, digest, size = line.rsplit(",", 2)
            if not digest:
                continue
            algo, _, value = digest.partition("=")
            data = archive.read(path)
            assert hashlib.new(algo, data).digest() == base64.urlsafe_b64decode(value + "=="), path
            assert int(size) == len(data), path
        assert any(n.endswith(".dist-info/WHEEL") for n in names)
        assert "Tag: py3-none-any" in archive.read(next(n for n in names if n.endswith(".dist-info/WHEEL"))).decode()
        junk = [n for n in names if re.search(r"\.pyc$|(^|/)(tests?|__pycache__|\.git[^/]*)(/|$)", n)]
        assert junk == []
        assert all(n.startswith("sslyze/") or ".dist-info/" in n for n in names)


def test_build_wheel_help_and_unknown_arguments_have_documented_exit_codes():
    help_result = subprocess.run(
        [str(SSLYZE_BUILD_SCRIPT), "--help"], capture_output=True, text=True, timeout=5, check=False
    )
    unknown_result = subprocess.run(
        [str(SSLYZE_BUILD_SCRIPT), "--not-a-valid-option"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert help_result.returncode == 0 and "Usage:" in help_result.stdout
    assert unknown_result.returncode == 2 and "Unknown option" in unknown_result.stderr


def test_build_wheel_selects_exactly_one_expected_filename(tmp_path: Path):
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    expected = output_dir / _sslyze_wheel().name
    expected.touch()
    selected = subprocess.run(
        ["bash", "-c", 'source "$1"; select_wheel "$2"', "bash", str(SSLYZE_BUILD_SCRIPT), str(output_dir)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert selected.returncode == 0 and selected.stdout == str(expected)

    extra = output_dir / "sslyze-9.9.9-py3-none-any.whl"
    extra.touch()
    multiple = subprocess.run(
        ["bash", "-c", 'source "$1"; select_wheel "$2"', "bash", str(SSLYZE_BUILD_SCRIPT), str(output_dir)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert multiple.returncode == 1 and "exactly one" in multiple.stderr


def _triplet_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    wheel = tmp_path / _sslyze_wheel().name
    wheel.write_bytes(_sslyze_wheel().read_bytes())
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        f"sslyze=={_lock_entries()['sslyze'][0]} \\\n    --hash=sha256:{_sha256(wheel)}\n",
        encoding="utf-8",
    )
    provenance = tmp_path / "PROVENANCE.json"
    provenance.write_text(PROVENANCE.read_text(encoding="utf-8"), encoding="utf-8")
    return wheel, lock, provenance


def _verify_triplet(wheel: Path, lock: Path, provenance: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; verify_wheel_triplet "$2" "$3" "$4" "python3.13"',
            "bash",
            str(SSLYZE_BUILD_SCRIPT),
            str(wheel),
            str(lock),
            str(provenance),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_build_wheel_triplet_checks_the_wheel_lock_and_provenance(tmp_path: Path):
    wheel, lock, provenance = _triplet_fixture(tmp_path)
    good = _verify_triplet(wheel, lock, provenance)
    assert good.returncode == 0, good.stderr

    lock.write_text(lock.read_text(encoding="utf-8").replace(_sha256(wheel), "0" * 64), encoding="utf-8")
    bad_lock = _verify_triplet(wheel, lock, provenance)
    assert bad_lock.returncode == 1 and "SHA-256" in bad_lock.stderr

    lock.write_text(
        f"sslyze=={_lock_entries()['sslyze'][0]} \\\n    --hash=sha256:{_sha256(wheel)}\n",
        encoding="utf-8",
    )
    data = json.loads(provenance.read_text(encoding="utf-8"))
    data["sha256"] = "0" * 64
    provenance.write_text(json.dumps(data), encoding="utf-8")
    bad_provenance = _verify_triplet(wheel, lock, provenance)
    assert bad_provenance.returncode == 1 and "SHA-256" in bad_provenance.stderr


def test_build_wheel_hardening_contract_is_enforced_in_ci_tests():
    text = SSLYZE_BUILD_SCRIPT.read_text(encoding="utf-8")
    assert 'BUILD_PYTHON_VERSION="3.13.5"' in text
    assert "umask 0002" in text
    assert "unshare --net --" in text
    assert 'flock -x "$BUILD_LOCK_FD"' in text
    assert 'mv -f -- "$temp_wheel" "$final_wheel"' in text
    assert 'sha256sum -- "$path"' in text
    assert 'find "$out"' not in text and "head -n 1" not in text


# --- installers: Dockerfile and CI ----------------------------------------------------------


def test_dockerfile_installs_wheels_only_from_lock_and_vendored_dir():
    text = DOCKERFILE.read_text(encoding="utf-8")
    install = next(m for m in re.finditer(r"RUN pip install(?:.|\\\n)*?requirements\.lock", text))
    command = install.group(0).replace("\\\n", " ")
    for flag in ("--require-hashes", "--only-binary=:all:", "--find-links /tmp/wheels", "-r /tmp/requirements.lock"):
        assert flag in command, f"Dockerfile install step lacks {flag}"
    assert "COPY docker/vendor/wheels/ /tmp/wheels/" in text
    assert "&& pip check" in text
    assert not re.search(r"pip install[^\n]*(--upgrade|setuptools|wheel)", text), (
        "no unpinned build tooling at install time"
    )
    assert not re.search(r"apt-get install[^&]*\b(gcc|g\+\+)\b", _dockerfile_stage(text, "deps-builder")), (
        "no compiler in the Python dependency stage"
    )
    assert "pip" in text.split("AS clean-deps")[1].split("AS app-source")[0], (
        "pip must still be stripped after pip check"
    )


def _dockerfile_stage(text: str, name: str) -> str:
    """Body of the named build stage, up to the next FROM."""
    match = re.search(rf"^FROM [^\n]* AS {re.escape(name)}\n(.*?)(?=^FROM |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"stage {name} not found in the Dockerfile"
    return match.group(1)


def test_libsignal_build_stage_is_isolated_with_a_pinned_rust_image():
    text = DOCKERFILE.read_text(encoding="utf-8")
    first_from = text.index("\nFROM ")
    default = re.search(r"^ARG RUST_IMAGE=(\S+)$", text[:first_from], re.MULTILINE)
    assert default, "RUST_IMAGE must be a global ARG with a default, declared before the first FROM"
    assert re.fullmatch(r"rust:\d+\.\d+\.\d+[\w.-]*@sha256:[0-9a-f]{64}", default.group(1)), (
        "RUST_IMAGE must name an exact Rust release and a digest"
    )
    assert re.search(
        r"^FROM --platform=\$BUILDPLATFORM \$\{RUST_IMAGE\} AS libsignal-build$", text, re.MULTILINE
    )
    # The compiler lives only in this stage; neither the Python dependency nor the runtime stage has one.
    build = _dockerfile_stage(text, "libsignal-build")
    for cross in ("gcc-aarch64-linux-gnu", "gcc-x86-64-linux-gnu"):
        assert cross in build, f"libsignal-build must be able to cross-compile with {cross}"
    assert "libsignal_jni_aarch64.so" in build and "libsignal_jni_amd64.so" in build, (
        "libsignal is built from source for both architectures"
    )
    for stage in ("deps-builder", "runtime"):
        body = _dockerfile_stage(text, stage)
        assert "libsignal-build" not in body and not re.search(r"apt-get install[^&]*\b(gcc|g\+\+|clang)", body)
    # Only the built library crosses into the final image, via the scratch stage.
    assert "COPY --from=libsignal-native / " in _dockerfile_stage(text, "signal-cli-dist")
    assert "COPY --from=libsignal-build" in _dockerfile_stage(text, "libsignal-native")
    # The upstream prebuilt libraries are replaced by the built one, never kept.
    assert "/libsignal-native/" in _dockerfile_stage(text, "signal-cli-dist")


def test_ci_installs_the_same_runtime_lock_and_vendored_wheel():
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(
        r"pip install --require-hashes --only-binary=:all: \\\n\s+"
        r"--find-links docker/vendor/wheels -r docker/requirements\.lock",
        text,
    )
    assert "pyproject-deps.py >> " not in text, "runtime dependencies must come from the lock, not be re-resolved"
    assert "feature/v1.0.0" in text and "workflow_dispatch" in text


# --- install failure modes (offline, no dependencies) --------------------------------------------


def _pip_install(requirements: Path, find_links: Path) -> subprocess.CompletedProcess:
    # A real install into a scratch --target: pip's --dry-run skips hash verification of
    # local wheels in some releases, which would let a tampered hash pass.
    target = tempfile.mkdtemp(dir=requirements.parent)
    command = [sys.executable, "-m", "pip", "install", "--target", target, "--no-deps", "--no-index"]
    command += ["--no-cache-dir", "--require-hashes", "--only-binary=:all:"]
    command += ["--find-links", str(find_links), "-r", str(requirements)]
    return subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)


def _sslyze_requirement(tmp_path: Path, digest: str) -> Path:
    version = _lock_entries()["sslyze"][0]
    path = tmp_path / "req.txt"
    path.write_text(f"sslyze=={version} --hash=sha256:{digest}\n", encoding="utf-8")
    return path


def test_install_accepts_locked_hash_and_rejects_tampered_hash_or_missing_wheel(tmp_path):
    good = _sslyze_requirement(tmp_path, _lock_entries()["sslyze"][1][0])
    assert _pip_install(good, WHEEL_DIR).returncode == 0, "control: the locked hash must install"

    bad = _sslyze_requirement(tmp_path, "0" * 64)
    assert _pip_install(bad, WHEEL_DIR).returncode != 0, "a wrong hash must abort the install"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _pip_install(good, empty).returncode != 0, "a missing wheel must abort the install"


def test_tampered_wheel_bytes_are_rejected(tmp_path):
    wheel = _sslyze_wheel()
    forged = tmp_path / "wheels"
    forged.mkdir()
    (forged / wheel.name).write_bytes(wheel.read_bytes() + b"\0")
    assert _pip_install(_sslyze_requirement(tmp_path, _sha256(wheel)), forged).returncode != 0


# --- release workflow: installed-version capture -------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("cryptography>=50.0.2", "cryptography"),
        ("Pillow", "pillow"),
        ("python_multipart", "python-multipart"),
        ("sslyze==6.3.1+justup.1", "sslyze"),
        ("uvicorn[standard]>=0.30; python_version >= '3.13'", "uvicorn"),
        ("sslyze @ https://example.invalid/pkg/sslyze-1.0.tar.gz", "sslyze"),
    ],
)
def test_distribution_name_is_parsed_from_requirement_expression(expression, expected):
    assert _load_tool("image-dependency-versions.py").distribution_name(expression) == expected


def test_installed_versions_are_read_for_expression_and_url_requirements(tmp_path):
    tool = _load_tool("image-dependency-versions.py")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\nversion = "9.9.9"\n'
        'dependencies = ["packaging>=24", "Pytest ; python_version >= \'3.13\'", "pip @ https://example.invalid/pip.tar.gz"]\n',
        encoding="utf-8",
    )
    lines = tool.collect(pyproject)
    values = dict(line.split("=", 1) for line in lines)
    assert values["app_version"] == "9.9.9"
    for name in ("packaging", "pytest", "pip"):
        assert re.fullmatch(r"\d+(\.\d+)*\S*", values[name]), values
    assert "packaging>=24" not in "".join(lines), "requirement expressions must not be used as keys"


def test_release_workflow_uses_the_name_parsing_script():
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "tools/image-dependency-versions.py" in text
    assert "metadata.version(dependency)" not in text


# --- relock tool: which wheels count for amd64 and arm64 -----------------------------------------------


@pytest.mark.parametrize(
    ("filename", "arches"),
    [
        ("nassl-5.4.0-cp313-cp313-manylinux2014_aarch64.manylinux_2_17_aarch64.whl", {"aarch64"}),
        ("cryptography-50.0.2-cp311-abi3-manylinux_2_28_x86_64.whl", {"x86_64"}),
        ("six-1.17.0-py2.py3-none-any.whl", {"x86_64", "aarch64"}),
        ("pkg-1.0-cp313-cp313-musllinux_1_2_x86_64.whl", set()),
        ("pkg-1.0-cp312-cp312-manylinux_2_28_x86_64.whl", set()),
        ("pkg-1.0-cp313-cp313t-manylinux_2_28_aarch64.whl", set()),
        ("pkg-1.0-cp313-cp313-manylinux_2_99_x86_64.whl", set()),
    ],
)
def test_relock_tool_selects_only_wheels_installable_in_the_image(filename, arches):
    assert _load_tool("relock-runtime.py").wheel_arches(filename) == arches


# --- release version shape: PR gate and release workflow must agree ---------------------------------

_VERSION_RE = re.compile(r"grep -qE '(\^\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\(-[^']*\)\?\$)'")


def _workflow_version_patterns(path: Path) -> list[str]:
    return _VERSION_RE.findall(path.read_text(encoding="utf-8"))


def test_release_version_pattern_is_identical_in_pr_gate_and_release_workflow():
    ci = _workflow_version_patterns(CI_WORKFLOW)
    release = _workflow_version_patterns(RELEASE_WORKFLOW)
    assert len(ci) == 1 and len(release) == 1
    assert ci == release


@pytest.mark.parametrize(
    ("version", "valid"),
    [
        ("1.0.0", True),
        ("1.0.0-rc.1", True),
        ("1.0.0-beta", True),
        ("1.0", False),
        ("1", False),
        ("v1.0.0", False),
        ("1.0.0-", False),
        ("1.0.0-rc..1", False),
        ("1.0.0+build", False),
        ("1.0.0 ", False),
        ("", False),
    ],
)
def test_release_version_pattern_accepts_only_release_shaped_versions(version, valid):
    (pattern,) = _workflow_version_patterns(RELEASE_WORKFLOW)
    assert bool(re.search(pattern, version)) is valid


def test_release_never_moves_latest_for_prerelease():
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    start = text.index('if [[ "$VERSION" == *-* ]]; then')
    prerelease_branch = text[start : text.index("elif", start)]
    assert ":latest" not in prerelease_branch
    assert text.index('"${IMAGE_NAME}:latest"', start) > start
