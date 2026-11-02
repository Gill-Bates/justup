#!/usr/bin/env python3
#
# tests/test_dockerhub.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Docker Hub helper used by the release workflow, with HTTP mocked."""

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "dockerhub", Path(__file__).resolve().parents[1] / "tools" / "dockerhub.py"
)
dockerhub = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dockerhub)

TAGS = "https://hub.docker.com/v2/namespaces/ns/repositories/repo/tags"


def _page(names, next_url=None):
    return json.dumps({"results": [{"name": n} for n in names], "next": next_url}).encode()


def _script(monkeypatch, responses):
    """Serve (status, body) tuples in order; record the calls."""
    calls = []
    queue = list(responses)

    def fake(method, url, token=None, body=None):  # noqa: ARG001 # mirrors _request
        calls.append((method, url))
        status, payload = queue.pop(0)
        if isinstance(status, Exception):
            raise status
        return status, payload

    monkeypatch.setattr(dockerhub, "_request", fake)
    monkeypatch.setattr(dockerhub.time, "sleep", lambda _s: None)
    return calls


def test_list_follows_pagination(monkeypatch):
    second = f"{TAGS}?page=2"
    _script(monkeypatch, [(200, _page(["a"], second)), (200, _page(["b", "c"]))])
    assert dockerhub.list_tags("ns", "repo", "t") == ["a", "b", "c"]


def test_list_error_on_later_page_aborts(monkeypatch):
    _script(monkeypatch, [(200, _page(["a"], f"{TAGS}?page=2")), (403, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.list_tags("ns", "repo", "t")


def test_list_404_first_page_only_when_allowed(monkeypatch):
    _script(monkeypatch, [(404, b"")])
    assert dockerhub.list_tags("ns", "repo", "t", allow_missing_repo=True) == []
    _script(monkeypatch, [(404, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.list_tags("ns", "repo", "t")


def test_list_404_on_later_page_is_an_error_even_when_allowed(monkeypatch):
    _script(monkeypatch, [(200, _page(["a"], f"{TAGS}?page=2")), (404, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.list_tags("ns", "repo", "t", allow_missing_repo=True)


def test_list_retries_server_errors_then_succeeds(monkeypatch):
    calls = _script(monkeypatch, [(503, b""), (429, b""), (200, _page(["a"]))])
    assert dockerhub.list_tags("ns", "repo", "t") == ["a"]
    assert len(calls) == 3


def test_list_gives_up_after_persistent_server_errors(monkeypatch):
    _script(monkeypatch, [(500, b"")] * dockerhub.GET_ATTEMPTS)
    with pytest.raises(dockerhub.HubError):
        dockerhub.list_tags("ns", "repo", "t")


def test_request_refuses_foreign_host():
    with pytest.raises(dockerhub.HubError):
        dockerhub._request("GET", "https://evil.example/v2/x", "secret")  # noqa: SLF001


@pytest.mark.parametrize(("status", "expected"), [(202, True), (204, True), (404, False)])
def test_delete_statuses(monkeypatch, status, expected):
    _script(monkeypatch, [(status, b"")])
    assert dockerhub.delete_tag("ns", "repo", "t", "1.0.0") is expected


def test_delete_other_status_raises(monkeypatch):
    _script(monkeypatch, [(500, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.delete_tag("ns", "repo", "t", "1.0.0")


def test_wait_polls_until_404(monkeypatch):
    calls = _script(monkeypatch, [(200, b""), (200, b""), (404, b"")])
    dockerhub.wait_for_tag_absent("ns", "repo", "t", "1.0.0")
    assert len(calls) == 3


def test_wait_fails_when_tag_stays_visible(monkeypatch):
    _script(monkeypatch, [(200, b"")] * dockerhub.VERIFY_ATTEMPTS)
    with pytest.raises(dockerhub.HubError):
        dockerhub.wait_for_tag_absent("ns", "repo", "t", "1.0.0")


def test_wait_fails_on_unexpected_status(monkeypatch):
    _script(monkeypatch, [(500, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.wait_for_tag_absent("ns", "repo", "t", "1.0.0")


def test_login_without_token_fails(monkeypatch):
    _script(monkeypatch, [(200, b"{}")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.login("u", "p")
    _script(monkeypatch, [(401, b"")])
    with pytest.raises(dockerhub.HubError):
        dockerhub.login("u", "p")


def test_login_accepts_access_token_key(monkeypatch):
    _script(monkeypatch, [(200, b'{"access_token": "abc"}')])
    assert dockerhub.login("u", "p") == "abc"


def test_cli_list_prints_tags_and_fails_closed(monkeypatch, capsys):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "ns")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "pw")
    monkeypatch.setenv("DOCKERHUB_REPOSITORY", "repo")
    _script(monkeypatch, [(200, b'{"token": "t"}'), (200, _page(["1.0.0", "latest"]))])
    assert dockerhub.main(["dockerhub.py", "list"]) == 0
    assert capsys.readouterr().out.split() == ["1.0.0", "latest"]
    _script(monkeypatch, [(200, b'{"token": "t"}'), (401, b"")])
    assert dockerhub.main(["dockerhub.py", "list"]) == 1


def test_cli_delete_verify_and_usage(monkeypatch):
    monkeypatch.setenv("DOCKERHUB_USERNAME", "ns")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "pw")
    _script(monkeypatch, [(200, b'{"token": "t"}'), (204, b""), (404, b""), (404, b"")])
    assert dockerhub.main(["dockerhub.py", "delete", "--verify", "1.0.0", "0.9.0"]) == 0
    assert dockerhub.main(["dockerhub.py", "delete"]) == 1
    assert dockerhub.main(["dockerhub.py", "list", "--bogus"]) == 1
