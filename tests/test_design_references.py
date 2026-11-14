#!/usr/bin/env python3
#
# tests/test_design_references.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""DC-REF-1: every file, spec and rule ID that DESIGN.md names resolves."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DOC = (_REPO / "DESIGN.md").read_text(encoding="utf-8")
_RULE = r"DC-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+"
_EXT = r"(?:py|js|mjs|css|html|md|yml|yaml|sh|json|toml|groovy|txt)"
# Directories a bare file name in the document may refer to (DESIGN.md 4.2 names owners by file name).
_BASES = (
    "",
    "app/",
    "app/api/",
    "app/utils/",
    "app/static/",
    "app/static/css/",
    "app/static/js/",
    "app/static/js/utils/",
    "app/static/js/charts/",
    "app/templates/",
    "app/templates/fragments/",
    "tests/",
    "tests/layout/",
    "tests/js/",
    "docs/",
)


def _tracked_names() -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.rsplit("/", 1)[-1] for line in listed.stdout.splitlines()}


def _open_requirements() -> str:
    """The table of 10.6 lists files that are allowed to be missing (DC-REF-1)."""
    return _DOC[_DOC.index("Open implementation requirements") :]


def test_files_and_specs_named_in_the_design_contract_exist() -> None:
    """A file-like token resolves in the repository, or is an open requirement of 10.6."""
    names = _tracked_names()
    allowed = _open_requirements()
    missing = []
    for token in re.findall(r"`([^`\n]+)`", _DOC):
        path = re.sub(r"::\w+.*$", "", token.strip()).split(" ")[0]
        if not re.fullmatch(rf"[\w./@-]+\.{_EXT}", path) or path.startswith((".", "Ju")):
            continue
        if any((_REPO / (base + path)).exists() for base in _BASES) or ("/" not in path and path in names):
            continue
        if path not in allowed and path.rsplit("/", 1)[-1] not in allowed:
            missing.append(path)
    assert not missing, f"DESIGN.md names files that do not exist: {sorted(set(missing))}"


def test_rule_ids_referenced_in_the_design_contract_are_defined_once() -> None:
    """Each cited rule ID has a definition, and a rule ID is never defined twice (DC-NORM-2)."""
    defined = re.findall(rf"^\s*(?:[-*]\s+)?\*\*(?:[^*(]*\()?({_RULE})\b", _DOC, re.MULTILINE)
    rows = re.findall(rf"^\|\s*({_RULE})\s*\|", _DOC, re.MULTILINE)
    duplicates = {rule for rule in defined if defined.count(rule) > 1}
    assert not duplicates, f"rule IDs defined more than once: {sorted(duplicates)}"
    known = set(defined) | set(rows)
    undefined = sorted(set(re.findall(_RULE, _DOC)) - known)
    assert not undefined, f"rule IDs referenced but never defined: {undefined}"
    areas = {re.sub(r"-\d+$", "", rule) for rule in known}
    unknown_areas = sorted(
        {f"DC-{a}" for a in re.findall(r"\bDC-([A-Z0-9]+)\b(?!-)", _DOC)}
        - {"DC-EX"}
        - {a for a in areas if "-" not in a[3:]}
    )
    assert not unknown_areas, f"area references without any rule: {unknown_areas}"
