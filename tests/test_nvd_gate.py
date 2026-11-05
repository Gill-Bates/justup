#
# tests/test_nvd_gate.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""tools/nvd-gate.py: SBOM generation and the blocking NVD check (offline, injected NVD answers)."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("nvd_gate", ROOT / "tools" / "nvd-gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)

PROVENANCE = (
    "nmap_version=7.98\n"
    "architecture=amd64\n"
    "bundled_libraries=libdnet=1.18.0,liblinear=2.50,libpcap=1.10.5\n"
    "bundled_libraries_origin=compiled into the binary\n"
)


def cve(cve_id, score=None, status="Analyzed", key="cvssMetricV31"):
    metrics = {key: [{"cvssData": {"baseScore": score}}]} if score is not None else {}
    return {"cve": {"id": cve_id, "vulnStatus": status, "metrics": metrics}}


def page(items, total=None, **extra):
    return {"vulnerabilities": items, "totalResults": len(items) if total is None else total, **extra}


class FakeNvd:
    """Maps the queried CPE to a list of answer pages; records every URL."""

    def __init__(self, by_cpe):
        self.by_cpe = by_cpe
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query, keep_blank_values=True)
        pages = self.by_cpe[query["virtualMatchString"][0]]
        return pages[int(query["startIndex"][0]) and 1 or 0] if len(pages) > 1 else pages[0]


NMAP_CPE = "cpe:2.3:a:nmap:nmap:7.98:*:*:*:*:*:*:*"
PCAP_CPE = "cpe:2.3:a:tcpdump:libpcap:1.10.5:*:*:*:*:*:*:*"


@pytest.fixture
def sbom_file(tmp_path):
    provenance = tmp_path / "PROVENANCE.txt"
    provenance.write_text(PROVENANCE, encoding="utf-8")
    out = tmp_path / "sbom.cdx.json"
    assert gate.main(["nvd-gate", "sbom", str(provenance), str(out)]) == 0
    return out


def run_check(sbom_file, nvd, *extra):
    return gate.main(["nvd-gate", "check", str(sbom_file), *extra], fetch=nvd)


def test_sbom_carries_exact_versions_cpes_and_marks_uncovered_components(sbom_file):
    sbom = json.loads(sbom_file.read_text(encoding="utf-8"))
    by_name = {c["name"]: c for c in sbom["components"]}
    assert sbom["bomFormat"] == "CycloneDX"
    assert by_name["nmap"]["cpe"] == NMAP_CPE
    assert by_name["nmap"]["purl"] == "pkg:generic/nmap@7.98"
    assert by_name["libpcap"]["cpe"] == PCAP_CPE
    for name in ("libdnet", "liblinear"):
        assert "cpe" not in by_name[name]
        assert "none" in by_name[name]["properties"][0]["value"]


@pytest.mark.parametrize(
    "text",
    [
        PROVENANCE.replace("nmap_version=7.98\n", ""),
        PROVENANCE.replace("7.98", "7.98-beta"),
        PROVENANCE.replace("libpcap=1.10.5", "libpcap=latest"),
        PROVENANCE.replace("liblinear=2.50,", ""),
        PROVENANCE.replace("libpcap=1.10.5", "libpcap=1.10.5,libnew=1.0"),
        PROVENANCE + "nmap_version=7.99\n",
    ],
)
def test_provenance_that_is_incomplete_or_ambiguous_is_rejected(tmp_path, text):
    provenance = tmp_path / "PROVENANCE.txt"
    provenance.write_text(text, encoding="utf-8")
    assert gate.main(["nvd-gate", "sbom", str(provenance), str(tmp_path / "out.json")]) == 3


def test_known_critical_cve_blocks_the_gate(sbom_file, tmp_path, capsys):
    nvd = FakeNvd({NMAP_CPE: [page([])], PCAP_CPE: [page([cve("CVE-2099-0001", 9.8)])]})
    report = tmp_path / "report.json"
    assert run_check(sbom_file, nvd, "--report", str(report)) == 1
    assert "BLOCKED: CVE-2099-0001 (CVSS 9.8) in libpcap 1.10.5" in capsys.readouterr().out
    assert json.loads(report.read_text(encoding="utf-8"))["blocking"][0]["cve"] == "CVE-2099-0001"
    assert any("virtualMatchString=cpe%3A2.3%3Aa%3Atcpdump%3Alibpcap%3A1.10.5" in url for url in nvd.urls)


def test_critical_cve_for_nmap_itself_blocks_via_cvss_v4(sbom_file):
    nvd = FakeNvd({NMAP_CPE: [page([cve("CVE-2099-0002", 9.3, key="cvssMetricV40")])], PCAP_CPE: [page([])]})
    assert run_check(sbom_file, nvd) == 1


def test_high_and_unscored_cves_do_not_block_but_unscored_is_reported(sbom_file, capsys):
    nvd = FakeNvd(
        {
            NMAP_CPE: [page([cve("CVE-2099-0003", 8.9), cve("CVE-2099-0004", None, status="Awaiting Analysis")])],
            PCAP_CPE: [page([cve("CVE-2099-0005", 9.9, status="Rejected")])],
        }
    )
    assert run_check(sbom_file, nvd) == 0
    output = capsys.readouterr().out
    assert "no CVSS v3/v4 score yet" in output and "CVE-2099-0004" in output
    assert "CVE-2099-0005" not in output
    assert "NOT COVERED: libdnet 1.18.0" in output and "NOT COVERED: liblinear 2.50" in output


def test_accepted_cve_needs_a_reason_and_then_passes(sbom_file, tmp_path):
    nvd = FakeNvd({NMAP_CPE: [page([cve("CVE-2099-0006", 9.8)])], PCAP_CPE: [page([])]})
    ignore = tmp_path / ".nvdignore"
    ignore.write_text("# header\nCVE-2099-0006\n", encoding="utf-8")
    assert run_check(sbom_file, nvd, "--ignore", str(ignore)) == 3
    ignore.write_text("CVE-2099-0006 only reachable through the unused rpcapd code\n", encoding="utf-8")
    assert run_check(sbom_file, nvd, "--ignore", str(ignore)) == 0


def test_repository_ignore_file_is_valid_and_has_no_hidden_acceptances():
    assert gate.load_ignores(ROOT / ".nvdignore") == {}


@pytest.mark.parametrize(
    "answer",
    [
        {"error": "x"},
        {"vulnerabilities": "no", "totalResults": 1},
        {"vulnerabilities": [], "totalResults": True},
        {"vulnerabilities": [{"cve": {"id": "not-a-cve"}}], "totalResults": 1},
        {"vulnerabilities": [], "totalResults": 3},
    ],
)
def test_malformed_or_inconsistent_nvd_answer_is_a_failure_not_a_pass(sbom_file, answer):
    assert run_check(sbom_file, FakeNvd({NMAP_CPE: [answer], PCAP_CPE: [page([])]})) == 3


def test_unreachable_nvd_fails_closed(sbom_file, capsys):
    def unreachable(url):
        raise gate.GateError("NVD answered HTTP 503")

    assert run_check(sbom_file, unreachable) == 3
    assert "not a clean result" in capsys.readouterr().err


def test_paging_collects_every_page_and_detects_a_critical_on_the_last_one(sbom_file):
    first = page([cve("CVE-2099-0007", 5.0)], total=2)
    second = page([cve("CVE-2099-0008", 9.1)], total=2)
    nvd = FakeNvd({NMAP_CPE: [first, second], PCAP_CPE: [page([])]})
    assert run_check(sbom_file, nvd) == 1
    assert len(nvd.urls) == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["components"].remove(next(c for c in s["components"] if c["name"] == "nmap")),
        lambda s: next(c for c in s["components"] if c["name"] == "nmap").pop("cpe"),
        lambda s: next(c for c in s["components"] if c["name"] == "nmap").update(cpe="cpe:2.3:a:nmap:nmap:*:*:*:*:*:*:*:*"),
        lambda s: s.update(bomFormat="SPDX"),
    ],
)
def test_sbom_that_would_check_nothing_for_nmap_is_rejected(sbom_file, mutate):
    sbom = json.loads(sbom_file.read_text(encoding="utf-8"))
    mutate(sbom)
    sbom_file.write_text(json.dumps(sbom), encoding="utf-8")
    assert run_check(sbom_file, FakeNvd({})) == 3
