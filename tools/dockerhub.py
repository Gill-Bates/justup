#!/usr/bin/env python3
#
# tools/dockerhub.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#
"""Docker Hub tag API used by the release workflow (standard library only).

Credentials come from DOCKERHUB_USERNAME / DOCKERHUB_TOKEN, the repository from
DOCKERHUB_REPOSITORY (default: justup); never from argv.
  list [--allow-missing-repo]   print every tag, one per line; an incomplete
                                listing is an error, never a partial result.
  delete [--verify] TAG...      delete tags; an absent tag is fine. --verify
                                polls until Docker Hub stops serving the tag.
Which tags to keep is decided by the workflow, not here.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HUB = "https://hub.docker.com"
LOGIN_URL = f"{HUB}/v2/users/login/"
RETRY_STATUSES = {429, 500, 502, 503, 504}
GET_ATTEMPTS = 5
VERIFY_ATTEMPTS = 12
VERIFY_DELAY = 5
RETRY_DELAY = 2


class HubError(RuntimeError):
    """Any Docker Hub failure that must stop the release step."""


def _request(method: str, url: str, token: str | None = None, body: bytes | None = None):
    """Return (status, body bytes); HTTP error statuses are returned, not raised."""
    if not url.startswith(HUB + "/"):
        raise HubError(f"refusing to call a non-Docker-Hub URL: {url}")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 # https only
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 # https only
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise HubError(f"{method} {url} failed: {err}") from err


def _get(url: str, token: str):
    """GET with retries on transport errors and 429/5xx."""
    for attempt in range(1, GET_ATTEMPTS + 1):
        try:
            status, body = _request("GET", url, token)
        except HubError:
            if attempt == GET_ATTEMPTS:
                raise
        else:
            if status not in RETRY_STATUSES or attempt == GET_ATTEMPTS:
                return status, body
        time.sleep(RETRY_DELAY)
    raise AssertionError("unreachable")


def login(username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password}).encode()
    status, body = _request("POST", LOGIN_URL, body=payload)
    if status != 200:
        raise HubError(f"Docker Hub login failed (HTTP {status})")
    data = json.loads(body)
    token = data.get("token") or data.get("access_token")
    if not token:
        raise HubError("Docker Hub login returned no token")
    return token


def _tags_url(namespace: str, repository: str) -> str:
    return f"{HUB}/v2/namespaces/{namespace}/repositories/{repository}/tags"


def list_tags(namespace: str, repository: str, token: str, allow_missing_repo: bool = False) -> list[str]:
    """All tags across every page. A 404 on the first page means no repository
    yet and is accepted only when asked for; any other failure raises."""
    first = url = f"{_tags_url(namespace, repository)}?page_size=100"
    tags: list[str] = []
    while url:
        status, body = _get(url, token)
        if status == 404 and url == first and allow_missing_repo:
            return []
        if status != 200:
            raise HubError(f"Docker Hub tag listing returned HTTP {status} for {url}")
        page = json.loads(body)
        tags.extend(entry["name"] for entry in page["results"])
        url = page.get("next") or ""
    return tags


def delete_tag(namespace: str, repository: str, token: str, tag: str) -> bool:
    """Delete one tag. Return False if it was already absent."""
    status, _ = _request("DELETE", f"{_tags_url(namespace, repository)}/{tag}", token)
    if status in (202, 204):
        return True
    if status == 404:
        return False
    raise HubError(f"failed to delete '{tag}' (HTTP {status})")


def wait_for_tag_absent(namespace: str, repository: str, token: str, tag: str) -> None:
    for _ in range(VERIFY_ATTEMPTS):
        status, _ = _request("GET", f"{_tags_url(namespace, repository)}/{tag}", token)
        if status == 404:
            return
        if status != 200:
            raise HubError(f"failed to verify deletion of '{tag}' (HTTP {status})")
        time.sleep(VERIFY_DELAY)
    raise HubError(f"tag still visible after deletion: {tag}")


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or args[0] not in ("list", "delete"):
        print(__doc__, file=sys.stderr)
        return 1
    command = args[0]
    flags = [a for a in args[1:] if a.startswith("--")]
    rest = [a for a in args[1:] if not a.startswith("--")]
    allowed = {"list": {"--allow-missing-repo"}, "delete": {"--verify"}}[command]
    if set(flags) - allowed or (command == "list" and rest) or (command == "delete" and not rest):
        print(__doc__, file=sys.stderr)
        return 1
    try:
        namespace = os.environ["DOCKERHUB_USERNAME"]
        password = os.environ["DOCKERHUB_TOKEN"]
        repository = os.environ.get("DOCKERHUB_REPOSITORY", "justup")
        token = login(namespace, password)
        if command == "list":
            tags = list_tags(namespace, repository, token, "--allow-missing-repo" in flags)
            for tag in tags:
                print(tag)
            return 0
        for tag in rest:
            if delete_tag(namespace, repository, token, tag):
                print(f"Deleted tag: {tag}")
                if "--verify" in flags:
                    wait_for_tag_absent(namespace, repository, token, tag)
                    print(f"Confirmed tag absent: {tag}")
            else:
                print(f"Tag already absent: {tag}")
    except (HubError, KeyError, ValueError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
