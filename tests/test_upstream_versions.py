from __future__ import annotations

import hashlib
import io
import json
from contextlib import nullcontext
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

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

    def download(url: str, destination: Path) -> str:
        assert url.endswith("/tar.gz/v1.104.4")
        destination.write_bytes(data)
        return checksum

    monkeypatch.setattr(upstream_versions, "_latest_tailscale_tag", lambda: "v1.104.4")
    monkeypatch.setattr(upstream_versions, "_tailscale_tag_commit", lambda tag: "b" * 40)
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


def test_latest_tag_uses_public_release_redirect(monkeypatch):
    def open_release(request, *, timeout):
        assert request.full_url == "https://github.com/tailscale/tailscale/releases/latest"
        assert request.get_method() == "HEAD"
        url = "https://github.com/tailscale/tailscale/releases/tag/v1.104.4"
        return nullcontext(SimpleNamespace(geturl=lambda: url))

    monkeypatch.setattr(upstream_versions.urllib.request, "urlopen", open_release)
    assert upstream_versions._latest_tailscale_tag() == "v1.104.4"


@pytest.mark.parametrize("annotated", [False, True])
def test_tag_lookup_records_commit_for_lightweight_and_annotated_tags(monkeypatch, annotated):
    tag_ref = "refs/tags/v1.104.4"
    commit = "b" * 40

    def ls_remote(args, **kwargs):
        assert args == [
            "git", "ls-remote", "--exit-code", "https://github.com/tailscale/tailscale.git",
            tag_ref, tag_ref + "^{}",
        ]
        if annotated:
            stdout = f"{'a' * 40}\t{tag_ref}\n{commit}\t{tag_ref}^{{}}\n"
        else:
            stdout = f"{commit}\t{tag_ref}\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(upstream_versions.subprocess, "run", ls_remote)
    assert upstream_versions._tailscale_tag_commit("v1.104.4") == commit


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
