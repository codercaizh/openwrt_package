from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
import shutil
import subprocess

import pytest

from owrt_builder.catalog import Catalog, PackageMetadata
from owrt_builder.devices import SourceSpec
from owrt_builder import sources as sources_module
from owrt_builder.sources import (
    SourceError,
    SourceManager,
    PREPARATION_VERSION,
    stage_download_seeds,
    validate_download_seeds,
)


def _catalog(root: Path) -> Catalog:
    return Catalog(
        root=root,
        packages=[PackageMetadata(name="demo", title="Demo")],
        authoritative=True,
        generated_files=("tmp/.packageinfo", "tmp/.config-package.in"),
    )


def _git_source_with_seed(root: Path, *, seed: bytes = b"trusted seed\n") -> tuple[Path, Path]:
    source = root / "source"
    source.mkdir(parents=True)
    seed_path = source / "dl" / "datconf-6bb733f7.tar.bz2"
    seed_path.parent.mkdir()
    seed_path.write_bytes(seed)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "dl"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
    return source, seed_path


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


def test_incompatible_go_refresh_keeps_previous_current_snapshot(tmp_path: Path) -> None:
    template = tmp_path / "template"
    module_dir = template / "package/passwall-packages/xray-core"
    module_dir.mkdir(parents=True)
    toolchain = template / "feeds/packages/lang/golang/golang"
    toolchain.mkdir(parents=True)
    (toolchain / "Makefile").write_text(
        "GO_VERSION_MAJOR_MINOR:=1.27\nGO_VERSION_PATCH:=0\n",
        encoding="utf-8",
    )
    (module_dir / "Makefile").write_text("GO_PKG:=github.com/xtls/xray-core\n", encoding="utf-8")
    module = module_dir / "go.mod"
    module.write_text("module github.com/xtls/xray-core\n\ngo 1.27\n", encoding="utf-8")
    spec = SourceSpec("test", "https://example.invalid/openwrt.git", "main", None, "arm")

    def runner(args, *, cwd=None, **kwargs):
        if args[1:2] == ["clone"]:
            shutil.copytree(template, Path(args[-1]))
        elif args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "source-sha\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager = SourceManager(tmp_path / "state", source_specs={"test": spec}, command_runner=runner)
    first = manager.prepare_source(
        "test",
        feed_preparer=lambda *_: {},
        catalog_builder=_catalog,
    )

    module.write_text("module github.com/xtls/xray-core\n\ngo 1.28\n", encoding="utf-8")
    with pytest.raises(SourceError, match="Go toolchain 1.27.0 is incompatible"):
        manager.prepare_source(
            "test",
            feed_preparer=lambda *_: {},
            catalog_builder=_catalog,
        )

    current = manager.current("test")
    assert current is not None
    assert current.snapshot_id == first.snapshot_id
    assert not list((tmp_path / "state" / "test").glob(".staging-*"))


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


def test_cleanup_preserves_tracked_download_seeds_and_removes_runtime_dl_files(tmp_path: Path) -> None:
    source, seed = _git_source_with_seed(tmp_path)
    runtime = source / "dl" / "runtime-download.tar.gz"
    runtime.write_bytes(b"generated during metadata preparation")
    (source / "dl" / "escape").symlink_to(tmp_path / "outside")

    SourceManager._clean_generated_tree(source)

    assert seed.read_bytes() == b"trusted seed\n"
    assert not runtime.exists()
    assert not (source / "dl" / "escape").exists()
    assert set(validate_download_seeds(source)) == {
        PurePosixPath("dl/datconf-6bb733f7.tar.bz2")
    }


def test_cleanup_removes_dl_when_source_has_no_tracked_seeds(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "dl").mkdir()
    (source / "dl" / "runtime-download.tar.gz").write_bytes(b"runtime")

    SourceManager._clean_generated_tree(source)

    assert not (source / "dl").exists()


def test_stage_download_seeds_validates_cache_root_without_tracked_seeds(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    cache = tmp_path / "cache" / "dl"
    cache.parent.mkdir(parents=True)
    cache.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(SourceError, match="must not be a symlink"):
        stage_download_seeds(source, cache)


def test_download_seed_cache_import_reuses_correct_and_replaces_wrong_file(tmp_path: Path) -> None:
    source, seed = _git_source_with_seed(tmp_path)
    SourceManager._clean_generated_tree(source)
    cache = tmp_path / "cache" / "dl"

    stage_download_seeds(source, cache)
    cached = cache / Path(*seed.relative_to(source).parts[1:])
    assert cached.read_bytes() == seed.read_bytes()
    original_mtime = cached.stat().st_mtime_ns

    stage_download_seeds(source, cache)
    assert cached.stat().st_mtime_ns == original_mtime

    cached.write_bytes(b"corrupt cache entry")
    stage_download_seeds(source, cache)
    assert cached.read_bytes() == seed.read_bytes()


def test_snapshot_validation_rejects_extra_download_files_and_symlinks(tmp_path: Path) -> None:
    source, seed = _git_source_with_seed(tmp_path)
    SourceManager._clean_generated_tree(source)
    snapshot = tmp_path / "state" / "test" / "snapshots" / "snapshot"
    snapshot_source = snapshot / "source"
    snapshot_source.parent.mkdir(parents=True)
    shutil.copytree(source, snapshot_source, symlinks=True)
    (snapshot / "catalog.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "manifest.json").write_text(
        f'{{"preparation_version": {PREPARATION_VERSION}, "source_id": "test", '
        '"snapshot_id": "snapshot", "source_commit": "source"}\n',
        encoding="utf-8",
    )
    manager = SourceManager(
        tmp_path / "state",
        source_specs={"test": SourceSpec("test", "https://example.invalid/openwrt.git", "main", None, "arm")},
    )

    assert manager._read_prepared(snapshot, expected_source_id="test").path == snapshot_source
    tracked_seed = snapshot_source / "dl" / "datconf-6bb733f7.tar.bz2"
    tracked_seed.write_bytes(b"tampered")
    with pytest.raises(SourceError, match="differs from Git HEAD"):
        manager._read_prepared(snapshot, expected_source_id="test")
    tracked_seed.write_bytes(seed.read_bytes())
    (snapshot_source / "dl" / "extra.tar.gz").write_bytes(b"extra")
    with pytest.raises(SourceError, match="untracked dl file"):
        manager._read_prepared(snapshot, expected_source_id="test")

    (snapshot_source / "dl" / "extra.tar.gz").unlink()
    (snapshot_source / "dl" / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(SourceError, match="symlink"):
        manager._read_prepared(snapshot, expected_source_id="test")


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
