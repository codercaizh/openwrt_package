"""Keep the OpenWrt Tailscale recipe on the latest stable upstream release.

The package's init/config files and build flags still come from OpenWrt.  Only
the source version, release and checksum change when its recipe lags upstream.
All network work happens during source preparation, before snapshot publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
import tempfile
from typing import Any
import urllib.request


class UpstreamVersionError(RuntimeError):
    """An upstream release cannot be used with the selected toolchain."""


def _get_json(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": "owrt-builder"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def _download(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "owrt-builder"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            digest.update(block)
            output.write(block)
    return digest.hexdigest()


def sync_latest_tailscale(
    source_root: Path, *, go_version: tuple[int, int, int] | None
) -> dict[str, str]:
    """Download, check and select the latest stable Tailscale source.

    Return its actual Git commit for the snapshot fingerprint, so a new
    release creates a new snapshot even if the OpenWrt recipe hasn't changed.
    A newer Go requirement fails here, before a long firmware compilation.
    """

    makefile = source_root / "feeds/packages/net/tailscale/Makefile"
    recipe = makefile.read_text(encoding="utf-8")
    release = _get_json("https://api.github.com/repos/tailscale/tailscale/releases/latest")
    tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise UpstreamVersionError(f"invalid stable Tailscale release: {tag!r}")
    version = tag[1:]
    commit = _get_json(f"https://api.github.com/repos/tailscale/tailscale/commits/{tag}").get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpstreamVersionError(f"Tailscale {tag} has no valid Git commit")

    with tempfile.TemporaryDirectory(prefix=".tailscale-release-", dir=source_root) as directory:
        archive = Path(directory) / f"tailscale-{version}.tar.gz"
        checksum = _download(
            f"https://codeload.github.com/tailscale/tailscale/tar.gz/{tag}", archive
        )
        with tarfile.open(archive, "r:gz") as tar:
            module = tar.extractfile(f"tailscale-{version}/go.mod")
            if module is None:
                raise UpstreamVersionError(f"Tailscale {tag} has no go.mod")
            manifest = module.read().decode("utf-8")
    match = re.search(r"(?m)^go (\d+\.\d+(?:\.\d+)?)\s*$", manifest)
    if not match:
        raise UpstreamVersionError(f"Tailscale {tag} has no Go version requirement")
    requirement = match.group(1)
    required = tuple(int(part) for part in requirement.split("."))
    required = (*required, 0) if len(required) == 2 else required
    if go_version is None or required > go_version:
        compiler = ".".join(map(str, go_version)) if go_version else "unknown"
        raise UpstreamVersionError(
            f"Tailscale {version} requires Go >= {requirement}; selected Go is {compiler}"
        )

    def assignment(name: str) -> str:
        matches = re.findall(rf"(?m)^{name}:=([^\s]+)\s*$", recipe)
        if len(matches) != 1:
            raise UpstreamVersionError(f"expected one {name} assignment in {makefile}")
        return matches[0]

    current = assignment("PKG_VERSION")
    current_checksum = assignment("PKG_HASH")
    current_release = assignment("PKG_RELEASE")
    if current == version and current_checksum != checksum:
        raise UpstreamVersionError(f"Tailscale {version} source checksum differs from OpenWrt recipe")
    for name, value in {
        "PKG_VERSION": version,
        "PKG_RELEASE": current_release if current == version else "1",
        "PKG_HASH": checksum,
    }.items():
        recipe = re.sub(rf"(?m)^{name}:=[^\r\n]+", f"{name}:={value}", recipe)
    temporary = makefile.with_suffix(".tmp")
    temporary.write_text(recipe, encoding="utf-8")
    temporary.replace(makefile)
    result = {
        "version": version, "commit": commit, "sha256": checksum,
        "go_required": requirement, "go_toolchain": ".".join(map(str, go_version)),
    }
    (makefile.parent / ".owrt-upstream.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    from .feeds import _go_toolchain_version

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path, help="unpublished OpenWrt source directory")
    args = parser.parse_args()
    result = sync_latest_tailscale(
        args.source_root, go_version=_go_toolchain_version(args.source_root)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
