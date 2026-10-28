from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from owrt_builder import upstream_versions
from owrt_builder.feeds import FeedSpec, prepare_feeds_sync


def _release(monkeypatch, *, requirement: str = "1.27.3") -> str:
    contents = f"module tailscale.com\n\ngo {requirement}\n".encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("tailscale-1.104.4/go.mod")
        member.size = len(contents)
        tar.addfile(member, io.BytesIO(contents))
    data = archive.getvalue()
    checksum = hashlib.sha256(data).hexdigest()

    def get_json(url: str):
        if url.endswith("/releases/latest"):
            return {"tag_name": "v1.104.4", "draft": False, "prerelease": False}
        assert url.endswith("/commits/v1.104.4")
        return {"sha": "b" * 40}

    def download(url: str, destination: Path) -> str:
        assert url.endswith("/tar.gz/v1.104.4")
        destination.write_bytes(data)
        return checksum

    monkeypatch.setattr(upstream_versions, "_get_json", get_json)
    monkeypatch.setattr(upstream_versions, "_download", download)
    return checksum


def _recipe(root: Path) -> Path:
    path = root / "feeds/packages/net/tailscale/Makefile"
    path.parent.mkdir(parents=True)
    path.write_text(
        "PKG_NAME:=tailscale\nPKG_VERSION:=1.104.1\nPKG_RELEASE:=2\n"
        f"PKG_HASH:={'a' * 64}\nGO_PKG:=tailscale.com/cmd/tailscaled\n"
        "GO_PKG_TAGS:=ts_include_cli,ts_omit_flashappliance\n"
    )
    return path


def test_latest_release_updates_recipe_and_checks_archive_go_requirement(tmp_path, monkeypatch):
    recipe = _recipe(tmp_path)
    checksum = _release(monkeypatch)
    result = upstream_versions.sync_latest_tailscale(tmp_path, go_version=(1, 27, 3))
    updated = recipe.read_text()
    assert "PKG_VERSION:=1.104.4\nPKG_RELEASE:=1\n" in updated
    assert f"PKG_HASH:={checksum}\n" in updated
    assert "GO_PKG_TAGS:=ts_include_cli,ts_omit_flashappliance\n" in updated
    assert result["commit"] == "b" * 40
    assert result["go_required"] == "1.27.3"
    assert json.loads((recipe.parent / ".owrt-upstream.json").read_text()) == result
    upstream_versions.sync_latest_tailscale(tmp_path, go_version=(1, 27, 3))
    assert recipe.read_text() == updated


def test_newer_go_requirement_rejects_release_before_recipe_changes(tmp_path, monkeypatch):
    recipe = _recipe(tmp_path)
    original = recipe.read_text()
    _release(monkeypatch, requirement="1.27.4")
    with pytest.raises(upstream_versions.UpstreamVersionError, match=r"requires Go >= 1\.27\.4"):
        upstream_versions.sync_latest_tailscale(tmp_path, go_version=(1, 27, 3))
    assert recipe.read_text() == original
    assert not (recipe.parent / ".owrt-upstream.json").exists()
    assert not list(tmp_path.glob(".tailscale-release-*"))


def test_feed_preparation_records_release_commit_alongside_recipe_commit(tmp_path, monkeypatch):
    import subprocess

    upstream = tmp_path / "upstream"
    recipe = _recipe(upstream)
    for args in (
        ["init", "-q", "-b", "master"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.invalid"],
        ["add", "."],
        ["commit", "-qm", "recipe"],
    ):
        subprocess.run(["git", *args], cwd=upstream, check=True)
    root = tmp_path / "prepared"
    golang = root / "feeds/packages/lang/golang/golang/Makefile"
    golang.parent.mkdir(parents=True)
    golang.write_text("GO_VERSION_MAJOR_MINOR:=1.27\nGO_VERSION_PATCH:=3\n")
    _release(monkeypatch)
    commits = prepare_feeds_sync(
        root,
        feed_specs=[FeedSpec(
            "openwrt-packages-tailscale", str(upstream), "feeds/packages/net/tailscale",
            branch="master", source_subdir=str(recipe.parent.relative_to(upstream)),
            clone_destination="staging/upstream/packages",
        )],
        require_native=False, require_metadata=False,
    )
    assert commits["tailscale-source"] == "b" * 40
    assert len(commits["openwrt-packages-tailscale"]) == 40
    assert "PKG_VERSION:=1.104.4" in (root / "feeds/packages/net/tailscale/Makefile").read_text()
    assert not (root / "staging/upstream/packages").exists()
