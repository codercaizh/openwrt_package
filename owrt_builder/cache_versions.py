"""Immutable cache provenance and ancestry selection within each repository."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
import re
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPOSITORY = "codercaizh/openwrt_package"
UPSTREAM = "immortalwrt/immortalwrt"
BRANCH = "immortalwrt-master"
REF = f"refs/heads/{BRANCH}"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def github(repo: str, resource: str, *, method: str = "GET"):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "owrt-cache"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"https://api.github.com/repos/{repo}/{resource}", headers=headers, method=method)
    with urlopen(req, timeout=35) as response:
        return None if response.status == 204 else json.load(response)


def inventory(prefix: str | None = None) -> list[dict]:
    result: list[dict] = []
    for page in range(1, 21):
        parameters = {"ref": REF, "per_page": 100, "page": page}
        if prefix is not None:
            parameters["key"] = prefix
        batch = github(REPOSITORY, "actions/caches?" + urlencode(parameters))["actions_caches"]
        result.extend(row for row in batch if row["ref"] == REF)
        if len(batch) < 100:
            return result
    raise ValueError("cache inventory exceeded pagination limit")


@lru_cache(maxsize=256)
def ancestor(repo: str, base: str, head: str) -> bool:
    if base == head:
        return True
    try:
        response = github(repo, f"compare/{base}...{head}?per_page=1")
    except HTTPError as exc:
        if exc.code in {404, 409}:
            return False
        raise
    return response["status"] in {"ahead", "identical"}


@dataclass(frozen=True)
class Version:
    builder: str
    upstream: str

    def precedes(self, other: Version, compare: Callable = ancestor) -> bool:
        return (
            compare(REPOSITORY, self.builder, other.builder)
            and compare(UPSTREAM, self.upstream, other.upstream)
        )


@dataclass(frozen=True)
class Candidate:
    key: str
    version: Version
    identity: str

    @classmethod
    def read(cls, prefix: str, key: str) -> Candidate | None:
        if not key.startswith(prefix):
            return None
        fields = key[len(prefix):].split("-")
        if (
            len(fields) != 3 or not SHA.fullmatch(fields[0]) or not SHA.fullmatch(fields[1])
            or not DIGEST.fullmatch(fields[2])
        ):
            return None
        return cls(key, Version(*fields[:2]), fields[2])


def maximal(candidates: list[Candidate], current: Version, compare: Callable = ancestor) -> list[Candidate]:
    compatible = [item for item in candidates if item.version.precedes(current, compare)]
    return [
        item for item in compatible
        if not any(item.version != other.version and item.version.precedes(other.version, compare)
                   for other in compatible)
    ]


def select(
    candidates: list[Candidate], current: Version, identity: str | None = None,
    compare: Callable = ancestor,
) -> Candidate | None:
    """Select a unique maximal compatible candidate without creation-time ordering."""
    choices = maximal(
        candidates if identity is None else [item for item in candidates if item.identity == identity],
        current, compare,
    )
    return choices[0] if len(choices) == 1 else None


def has_newer(candidates: list[Candidate], current: Version, compare: Callable = ancestor) -> bool:
    return any(current != item.version and current.precedes(item.version, compare) for item in candidates)
