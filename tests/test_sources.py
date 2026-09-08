from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from owrt_builder.catalog import Catalog, PackageMetadata
from owrt_builder.devices import SourceSpec
from owrt_builder import sources as sources_module
from owrt_builder.sources import SourceError, SourceManager


def _catalog(root: Path) -> Catalog:
    return Catalog(
        root=root,
        packages=[PackageMetadata(name="demo", title="Demo")],
        authoritative=True,
        generated_files=("tmp/.packageinfo", "tmp/.config-package.in"),
    )


def test_source_publish_is_atomic_and_failed_refresh_keeps_current(tmp_path: Path) -> None:
    template = tmp_path / "template"
    template.mkdir()
    for name in ("build_dir", "staging_dir", "tmp", "dl", "logs", "bin"):
        (template / name).mkdir()
    spec = SourceSpec(
        id="test",
        url="https://example.invalid/openwrt.git",
        branch="main",
        snapshot=None,
        platform="arm",
    )
    calls = {"prepare": 0}

    def runner(args, *, cwd=None, **kwargs):
        if args[1:2] == ["clone"]:
            destination = Path(args[-1])
            shutil.copytree(template, destination)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "source-sha\n", "")
        raise AssertionError(args)

    manager = SourceManager(
        tmp_path / "state",
        source_specs={"test": spec},
        command_runner=runner,
    )
    stale = tmp_path / "state" / "test" / ".staging-crashed" / "source"
    stale.mkdir(parents=True)
    (stale / "partial").write_text("left by killed preparation\n", encoding="utf-8")
    current_tmp = tmp_path / "state" / "test" / ".current.crashed.tmp"
    current_tmp.symlink_to("/container/temporary/snapshot")

    def prepare(root: Path, _status):
        calls["prepare"] += 1
        return {"custom": "feed-sha"}

    first = manager.prepare_source(
        "test",
        feed_preparer=prepare,
        catalog_builder=_catalog,
    )
    assert manager.current("test").snapshot_id == first.snapshot_id
    assert all(not (first.path / name).exists() for name in ("build_dir", "staging_dir", "tmp", "dl", "logs", "bin"))
    assert first.catalog_path.is_file()
    assert first.snapshot_id == manager.get_snapshot(first.snapshot_id).snapshot_id
    assert not (tmp_path / "state" / "test" / ".staging-crashed").exists()
    assert not current_tmp.is_symlink() and not current_tmp.exists()

    current_before = manager.current("test")

    def failed_prepare(root: Path, _status):
        raise RuntimeError("feed network unavailable")

    with pytest.raises(SourceError):
        manager.prepare_source(
            "test",
            feed_preparer=failed_prepare,
            catalog_builder=_catalog,
        )
    current_after = manager.current("test")
    assert current_before is not None and current_after is not None
    assert current_after.snapshot_id == current_before.snapshot_id
    assert calls["prepare"] == 1
    assert not list((tmp_path / "state" / "test").glob(".staging-*"))


def test_invalid_content_address_is_rebuilt(tmp_path: Path) -> None:
    template = tmp_path / "template"
    template.mkdir()
    spec = SourceSpec("test", "https://example.invalid/openwrt.git", "main", None, "arm")

    def runner(args, *, cwd=None, **kwargs):
        if args[1:2] == ["clone"]:
            shutil.copytree(template, Path(args[-1]))
        elif args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "source-sha\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager = SourceManager(tmp_path / "state", source_specs={"test": spec}, command_runner=runner)
    first = manager.prepare_source("test", feed_preparer=lambda *_: {}, catalog_builder=_catalog)
    snapshot_dir = first.path.parent
    (snapshot_dir / "manifest.json").unlink()

    rebuilt = manager.prepare_source("test", feed_preparer=lambda *_: {}, catalog_builder=_catalog)
    assert rebuilt.snapshot_id == first.snapshot_id
    assert rebuilt.catalog_path.is_file()
    assert manager.current("test").snapshot_id == first.snapshot_id


def test_preparation_version_is_part_of_snapshot_identity(tmp_path: Path, monkeypatch) -> None:
    template = tmp_path / "template"
    template.mkdir()
    spec = SourceSpec("test", "https://example.invalid/openwrt.git", "main", None, "arm")

    def runner(args, *, cwd=None, **kwargs):
        if args[1:2] == ["clone"]:
            shutil.copytree(template, Path(args[-1]))
        elif args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "source-sha\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager = SourceManager(tmp_path / "state", source_specs={"test": spec}, command_runner=runner)
    first = manager.prepare_source("test", feed_preparer=lambda *_: {"feed": "sha"}, catalog_builder=_catalog)

    monkeypatch.setattr(sources_module, "PREPARATION_VERSION", sources_module.PREPARATION_VERSION + 1)
    second = manager.prepare_source("test", feed_preparer=lambda *_: {"feed": "sha"}, catalog_builder=_catalog)
    assert second.snapshot_id != first.snapshot_id
    assert manager.current("test").snapshot_id == second.snapshot_id


def test_cleanup_removes_feed_runtime_links_and_indexes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    feeds = source / "feeds"
    feeds.mkdir(parents=True)
    (feeds / "base").symlink_to("/container/temporary/source/package")
    (feeds / "packages.index").symlink_to("packages.tmp/.packageinfo")
    (feeds / "packages.targetindex").symlink_to("packages.tmp/.targetinfo")
    (feeds / "packages.tmp").mkdir()
    (feeds / "routing.index").write_text("stale\n", encoding="utf-8")
    (feeds / "telephony.targetindex").mkdir()
    (feeds / "stale.tmp").symlink_to("/container/temporary/feed")
    (feeds / "base").unlink()
    (feeds / "base").mkdir()
    (feeds / "packages").mkdir()

    SourceManager._clean_generated_tree(source)

    assert not (feeds / "base").is_symlink()
    assert not (feeds / "packages.index").is_symlink()
    assert not (feeds / "packages.targetindex").is_symlink()
    assert not (feeds / "routing.index").exists()
    assert not (feeds / "telephony.targetindex").exists()
    assert not (feeds / "base").exists()
    assert not (feeds / "packages.tmp").exists()
    assert not (feeds / "stale.tmp").exists()
    assert (feeds / "packages").is_dir()


def test_current_rejects_external_snapshot_and_manifest_directory_mismatch(tmp_path: Path) -> None:
    state = tmp_path / "state"
    source_root = state / "test"
    snapshots = source_root / "snapshots"
    snapshots.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "source").mkdir(parents=True)
    (outside / "catalog.json").write_text("{}\n", encoding="utf-8")
    (outside / "manifest.json").write_text(
        '{"preparation_version": 5, "source_id": "test", '
        '"snapshot_id": "external", "source_commit": "source"}\n',
        encoding="utf-8",
    )
    (source_root / "current").symlink_to(outside)

    spec = SourceSpec("test", "https://example.invalid/openwrt.git", "main", None, "arm")
    manager = SourceManager(state, source_specs={"test": spec})
    assert manager.current("test") is None
    with pytest.raises(KeyError):
        manager.get_snapshot("external")

    valid = snapshots / "directory-name"
    (valid / "source").mkdir(parents=True)
    (valid / "catalog.json").write_text("{}\n", encoding="utf-8")
    (valid / "manifest.json").write_text(
        '{"preparation_version": 5, "source_id": "test", '
        '"snapshot_id": "different-name", "source_commit": "source"}\n',
        encoding="utf-8",
    )
    with pytest.raises(SourceError):
        manager._read_prepared(valid, expected_source_id="test")
