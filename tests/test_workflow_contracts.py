#!/usr/bin/env python3
#
# tests/test_workflow_contracts.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Cross-file contracts of the release workflows that no single tool test covers."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = (WORKFLOWS / "docker-build.yml").read_text(encoding="utf-8")
SECURITY = (WORKFLOWS / "security.yml").read_text(encoding="utf-8")


def matrix_platforms() -> list[str]:
    block = RELEASE.split("jq -c . <<'JSON'", 1)[1].split("JSON", 1)[0]
    return sorted(entry["platform"] for entry in json.loads(block)["include"])


def test_security_rescan_expects_the_release_matrix_platforms():
    expected = re.search(r"EXPECTED_PLATFORMS: '([^']+)'", SECURITY).group(1).split()
    assert sorted(expected) == matrix_platforms()


def test_security_rescan_is_read_only():
    assert "permissions:\n  contents: read" in SECURITY
    assert not re.search(r"docker/login-action|imagetools create|docker push|:\s*write", SECURITY)


def test_purity_gate_runs_before_the_registry_push():
    purity = RELEASE.index("< tools/verify-runtime-image.py")
    assert RELEASE.index("tools/verify-runtime-image.py layers") > purity
    assert purity < RELEASE.index("Log in to Docker Hub (push)")


def test_version_tag_is_published_only_from_the_verified_candidate():
    candidate = RELEASE.index("Create candidate index")
    verified = RELEASE.index("tools/verify-manifest.py verify manifest-candidate.json")
    version = RELEASE.index('imagetools create --tag "${IMAGE_NAME}:${VERSION}"')
    assert candidate < verified < version
    assert RELEASE.count('imagetools create --tag "${IMAGE_NAME}:${VERSION}"') == 1
    assert "@${INDEX_DIGEST}" in RELEASE


def test_temporary_tag_cleanup_does_not_depend_on_artifacts():
    cleanup = RELEASE.split("  cleanup-arch-tags:", 1)[1].split("  cleanup-dockerhub-tags:", 1)[0]
    assert "download-artifact" not in cleanup
    assert "needs.meta.outputs.archs" in cleanup and "CANDIDATE_TAG" in cleanup


def test_no_trivy_result_is_defaulted_to_zero():
    assert not re.search(r"\|\|\s*echo 0", RELEASE + SECURITY)


def test_published_version_tag_is_checked_before_it_is_written():
    publish = RELEASE.index("      - name: Publish version tag")
    assert (
        publish
        < RELEASE.index('dockerhub.py exists "$VERSION"')
        < RELEASE.index('imagetools create --tag "${IMAGE_NAME}:${VERSION}"')
    )


def test_registry_is_rechecked_right_before_latest_moves_and_before_tags_are_deleted():
    promote = RELEASE.split("      - name: Promote latest", 1)[1].split("  cleanup-arch-tags:", 1)[0]
    assert promote.index("release-newest.py") < promote.index('imagetools create --tag "${IMAGE_NAME}:latest"')
    cleanup = RELEASE.split("  cleanup-dockerhub-tags:", 1)[1].split("  create-release:", 1)[0]
    assert cleanup.index("release-newest.py") < cleanup.index("dockerhub.py delete")


def test_nvd_gate_and_prepared_source_archive_run_before_the_registry_push():
    push = RELEASE.index("Log in to Docker Hub (push)")
    assert RELEASE.index("tools/nvd-gate.py check") < push
    assert RELEASE.index("tools/release-publish.py source") < push
    # The gate must not swallow its exit status: a CRITICAL CVE (1) or an NVD failure (3) fails the job.
    assert 'exit "$status"' in RELEASE


def test_release_is_a_verified_draft_before_it_is_published():
    release = RELEASE.split("\n  create-release:", 1)[1]
    steps = [
        release.index("tools/release-publish.py plan"),
        release.index("-F draft=true"),
        release.index("releases/${RELEASE_ID}/assets?name="),
        release.index("tools/release-publish.py verify release-draft.json"),
        release.index("-F draft=false"),
        release.index("tools/release-publish.py verify release-published.json"),
    ]
    assert steps == sorted(steps)
    # A published tag is never edited, replaced or deleted by the workflow.
    assert "createRelease" not in release and "updateRelease" not in release
    # The only delete is the removal of stale assets the plan listed for an unpublished draft.
    assert release.count("-X DELETE") == 1
    assert release.index("jq -r '.delete[] | .id' release-plan.json") < release.index("-X DELETE")


def test_contents_write_is_confined_to_the_release_job():
    for job in re.split(r"\n  (?=[a-z][a-z-]*:\n)", RELEASE.split("\njobs:\n", 1)[1]):
        name = job.split(":", 1)[0]
        assert ("contents: write" in job) == (name == "create-release"), name
