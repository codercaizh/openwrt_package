from __future__ import annotations

import json
from pathlib import Path

import pytest

from owrt_builder.build import BuildRequest, logical_cpu_count
from owrt_builder.cache import BuildCacheError, BuildCacheManager


def _resolver(value: str) -> str:
    return {
        "n60pro": "netcore_n60-pro",
        "n60-pro": "netcore_n60-pro",
        "netcore_n60-pro": "netcore_n60-pro",
    }.get(value, value)


def _ready_entry(manager: BuildCacheManager, device: str, task: str, *, created: str) -> Path:
    root = manager._entry_path(device)
    (root / "openwrt").mkdir(parents=True, exist_ok=True)
    (root / "openwrt" / "sample").write_bytes(b"cache")
    manager.touch(
        device,
        task_id=task,
        source_id="source",
        snapshot_id="snapshot",
        source_commit="commit",
        config_sha256="config",
        ready=True,
    )
    metadata = json.loads((root / "cache.json").read_text(encoding="utf-8"))
    metadata["created_at"] = created
    metadata["last_used_at"] = "2099-01-01T00:00:00+00:00"
    (root / "cache.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


def test_build_request_jobs_default_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("owrt_builder.build.logical_cpu_count", lambda: 4)
    assert BuildRequest(device="n60pro").jobs == 4
    assert BuildRequest(device="n60pro", jobs=1).jobs == 1
    assert BuildRequest(device="n60pro", jobs=4).jobs == 4
    for value in (0, 5, True, "2"):
        with pytest.raises(ValueError):
            BuildRequest(device="n60pro", jobs=value)  # type: ignore[arg-type]


def test_device_cache_reuses_across_snapshot_updates_and_preserves_incremental_dirs(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    with manager.acquire(
        "n60pro",
        "first",
        source_id="source",
        snapshot_id="old-snapshot",
        legacy_aliases=("n60pro",),
    ) as lease:
        lease.source_root.mkdir(parents=True)
        (lease.source_root / "old-source").write_text("old", encoding="utf-8")
        (lease.source_root / "build_dir").mkdir()
        (lease.source_root / "build_dir" / "object").write_text("keep", encoding="utf-8")
        (lease.source_root / "staging_dir").mkdir()
        (lease.source_root / "staging_dir" / "object").write_text("keep", encoding="utf-8")
        manager.touch(
            lease.device,
            task_id=lease.task_id,
            source_id="source",
            snapshot_id="old-snapshot",
            source_commit="old-commit",
            config_sha256="old-config",
            ready=True,
        )

    prepared = tmp_path / "prepared"
    prepared.mkdir()
    (prepared / "new-source").write_text("new", encoding="utf-8")
    with manager.acquire(
        "netcore_n60-pro",
        "second",
        source_id="source",
        snapshot_id="new-snapshot",
        reuse=True,
    ) as lease:
        assert lease.reused is True
        manager.refresh_source(
            lease,
            prepared,
            source_id="source",
            snapshot_id="new-snapshot",
            source_commit="new-commit",
        )
        assert (lease.source_root / "new-source").read_text(encoding="utf-8") == "new"
        assert (lease.source_root / "build_dir" / "object").read_text(encoding="utf-8") == "keep"
        assert (lease.source_root / "staging_dir" / "object").read_text(encoding="utf-8") == "keep"
        assert lease.source_root.joinpath("dl").is_symlink()
        manager.touch(
            lease.device,
            task_id=lease.task_id,
            source_id="source",
            snapshot_id="new-snapshot",
            source_commit="new-commit",
            config_sha256="new-config",
            ready=True,
        )

    metadata = json.loads(
        (tmp_path / "cache" / "builds" / "netcore_n60-pro" / "cache.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["snapshot_id"] == "new-snapshot"
    assert metadata["ready"] is True


def test_legacy_download_migration_drops_symlinks_and_keeps_regular_files(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1)
    destination = tmp_path / "cache-entry" / "openwrt"
    dl = destination / "dl"
    dl.mkdir(parents=True)
    (dl / "trusted.tar.gz").write_bytes(b"trusted")
    external = tmp_path / "outside-download"
    external.write_bytes(b"must stay outside the cache")
    (dl / "escape.tar.gz").symlink_to(external)
    (dl / "unsafe-directory").mkdir()
    (dl / "unsafe-directory" / "file").write_bytes(b"runtime")
    cache = tmp_path / "cache" / "dl"
    cache.mkdir(parents=True)
    (cache / "escape.tar.gz").symlink_to(external)

    manager._ensure_download_link(destination)

    assert (cache / "trusted.tar.gz").read_bytes() == b"trusted"
    assert not (cache / "escape.tar.gz").exists()
    assert not (cache / "unsafe-directory").exists()
    assert external.read_bytes() == b"must stay outside the cache"
    assert (destination / "dl").is_symlink()
    assert (destination / "dl" / "trusted.tar.gz").read_bytes() == b"trusted"


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_download_cache_root_must_be_real_directory_without_tracked_seeds(
    tmp_path: Path, kind: str
) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1)
    cache = tmp_path / "cache" / "dl"
    cache.parent.mkdir(parents=True)
    if kind == "symlink":
        cache.symlink_to(tmp_path, target_is_directory=True)
    else:
        cache.write_text("not a directory", encoding="utf-8")
    destination = tmp_path / "cache-entry" / "openwrt"
    destination.mkdir(parents=True)

    with pytest.raises(BuildCacheError, match="download cache"):
        manager._ensure_download_link(destination)


def test_new_cache_stays_not_ready_until_build_finishes(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1)
    with manager.acquire("device", "failed-task", source_id="source", snapshot_id="snapshot") as lease:
        lease.source_root.mkdir(parents=True)
        # Simulate a compile failure: no final update_source(..., ready=True).
    metadata = json.loads(
        (tmp_path / "cache" / "builds" / "device" / "cache.json").read_text(encoding="utf-8")
    )
    assert metadata["ready"] is False
    with manager.acquire("device", "retry-task", source_id="source", snapshot_id="snapshot") as retry:
        assert retry.reused is False


def test_fifo_eviction_uses_created_at_and_logs_actual_space(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1)
    old = _ready_entry(manager, "old-device", "old-task", created="2020-01-01T00:00:00+00:00")
    new = _ready_entry(manager, "new-device", "new-task", created="2021-01-01T00:00:00+00:00")
    available = iter((0, 128, 128))
    manager._disk_free = lambda: next(available, 128)  # type: ignore[method-assign]
    lines: list[str] = []

    assert manager.ensure_capacity(100, callback=lines.append) is True
    assert not old.exists()
    assert new.exists()
    assert any("old-device" in line and "free_before=0" in line and "free_after=128" in line for line in lines)


def test_eviction_skips_busy_device_lock_and_warns_when_space_is_insufficient(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1)
    old = _ready_entry(manager, "old-device", "old-task", created="2020-01-01T00:00:00+00:00")
    available = iter((0, 0, 0))
    manager._disk_free = lambda: next(available, 0)  # type: ignore[method-assign]
    lines: list[str] = []
    with manager._device_lock("old-device", timeout=0):
        assert manager.ensure_capacity(100, callback=lines.append) is False
    assert old.exists()
    assert any("设备锁忙" in line for line in lines)
    assert any("估算仍不足" in line for line in lines)


def test_legacy_build_tree_is_moved_without_copy_when_provenance_and_links_are_safe(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    task = "legacy-task"
    legacy = tmp_path / "builds" / task / "openwrt"
    legacy.mkdir(parents=True)
    (legacy / "source-file").write_text("compiled", encoding="utf-8")
    (tmp_path / "tasks" / task).mkdir(parents=True)
    (tmp_path / "tasks" / task / "request.json").write_text(
        json.dumps({"device": "n60pro"}), encoding="utf-8"
    )
    (tmp_path / "tasks" / task / "result.json").write_text(
        json.dumps(
            {
                "status": "success",
                "device": "netcore_n60-pro",
                "source_id": "source",
                "snapshot_id": "snapshot",
                "source_commit": "commit",
                "config_sha256": "config",
                "started_at": "2020-01-01T00:00:00+00:00",
                "finished_at": "2020-01-01T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    with manager.acquire(
        "n60pro",
        "new-task",
        source_id="source",
        snapshot_id="snapshot",
        legacy_aliases=("n60pro",),
    ) as lease:
        assert lease.reused is True
        assert lease.source_root.joinpath("source-file").read_text(encoding="utf-8") == "compiled"
        assert not legacy.exists()
        assert lease.root.joinpath("cache.json").is_file()


def test_unsafe_legacy_absolute_link_is_skipped(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    task = "legacy-unsafe"
    legacy = tmp_path / "builds" / task / "openwrt"
    legacy.mkdir(parents=True)
    (legacy / "bad").symlink_to("/work/package")
    (tmp_path / "tasks" / task).mkdir(parents=True)
    (tmp_path / "tasks" / task / "result.json").write_text(
        json.dumps(
            {
                "status": "success",
                "device": "n60pro",
                "source_id": "source",
                "snapshot_id": "snapshot",
            }
        ),
        encoding="utf-8",
    )
    lines: list[str] = []
    with manager.acquire(
        "n60pro",
        "new-task",
        source_id="source",
        snapshot_id="snapshot",
        legacy_aliases=("n60pro",),
        callback=lines.append,
    ) as lease:
        assert lease.reused is False
    # The unsafe tree is rejected for migration, then reclaimed by the
    # same-device cleanup path.  Link validation must not make old compiler
    # storage impossible to collect.
    assert not legacy.exists()
    assert any("不安全" in line for line in lines)


def test_unsafe_legacy_is_fifo_evicted_without_following_external_target(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    task = "legacy-unsafe-eviction"
    legacy = tmp_path / "builds" / task / "openwrt"
    legacy.mkdir(parents=True)
    external = tmp_path / "outside-target"
    external.write_text("keep", encoding="utf-8")
    (legacy / "bad").symlink_to(external)
    (tmp_path / "tasks" / task).mkdir(parents=True)
    (tmp_path / "tasks" / task / "result.json").write_text(
        json.dumps(
            {
                "status": "success",
                "device": "n60pro",
                "source_id": "source",
                "snapshot_id": "snapshot",
            }
        ),
        encoding="utf-8",
    )

    # It is not eligible for reuse, even though final task evidence exists.
    assert (
        manager._legacy_candidate(
            "n60pro", {"n60pro"}, "source", "snapshot"
        )
        is None
    )

    available = iter((0, 128))
    manager._disk_free = lambda: next(available, 128)  # type: ignore[method-assign]
    lines: list[str] = []
    assert manager.ensure_capacity(100, callback=lines.append) is True
    assert not legacy.exists()
    assert external.read_text(encoding="utf-8") == "keep"
    assert any("删除编译缓存" in line and str(legacy) in line for line in lines)


def test_legacy_container_absolute_links_are_rewritten_relative(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    task = "legacy-links"
    legacy = tmp_path / "builds" / task / "openwrt"
    legacy.mkdir(parents=True)
    (legacy / "target").write_text("target", encoding="utf-8")
    (legacy / "link").symlink_to(f"/workspace/work/builds/{task}/openwrt/target")
    (tmp_path / "tasks" / task).mkdir(parents=True)
    (tmp_path / "tasks" / task / "result.json").write_text(
        json.dumps(
            {
                "status": "success",
                "device": "n60pro",
                "source_id": "source",
                "snapshot_id": "snapshot",
            }
        ),
        encoding="utf-8",
    )
    with manager.acquire(
        "n60pro",
        "new-task",
        source_id="source",
        snapshot_id="snapshot",
        legacy_aliases=("n60pro",),
    ) as lease:
        assert lease.reused is True
        link = lease.source_root / "link"
        assert not Path(link.readlink()).is_absolute()
        assert link.resolve().read_text(encoding="utf-8") == "target"


def test_legacy_reuse_prefers_newest_completed_candidate(tmp_path: Path) -> None:
    manager = BuildCacheManager(tmp_path, default_estimate_bytes=1, device_resolver=_resolver)
    for task, marker, when in (
        ("legacy-old", "old", ("2020-01-01T00:00:00+00:00", "2020-01-01T01:00:00+00:00")),
        ("legacy-new", "new", ("2021-01-01T00:00:00+00:00", "2021-01-01T01:00:00+00:00")),
    ):
        tree = tmp_path / "builds" / task / "openwrt"
        tree.mkdir(parents=True)
        (tree / "marker").write_text(marker, encoding="utf-8")
        task_dir = tmp_path / "tasks" / task
        task_dir.mkdir(parents=True)
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "device": "n60pro",
                    "source_id": "source",
                    "snapshot_id": "snapshot",
                    "started_at": when[0],
                    "finished_at": when[1],
                }
            ),
            encoding="utf-8",
        )

    with manager.acquire(
        "n60pro",
        "new-task",
        source_id="source",
        snapshot_id="snapshot",
        legacy_aliases=("n60pro",),
    ) as lease:
        assert lease.reused is True
        assert (lease.source_root / "marker").read_text(encoding="utf-8") == "new"
