from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import shutil

import pytest

from owrt_builder import cache_archive
from owrt_builder import ci_cache as ci
from owrt_builder.cache_versions import Candidate, REPOSITORY, UPSTREAM, Version, has_newer, select
from owrt_builder.devices import SourceSpec
from owrt_builder.source_sync import retain_mtimes, sync_source
from owrt_builder.sources import SourceManager


def command(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def make_git(root: Path) -> SourceSpec:
    root.mkdir()
    command("git", "init", "-q", "-b", "master", cwd=root)
    command("git", "config", "user.name", "fixture", cwd=root)
    command("git", "config", "user.email", "fixture@example.net", cwd=root)
    (root / "tools/llvm-bpf").mkdir(parents=True)
    (root / "tools/llvm-bpf/Makefile").write_text("llvm")
    (root / "rules.mk").write_text("common")
    command("git", "add", ".", cwd=root)
    command("git", "commit", "-qm", "initial", cwd=root)
    return SourceSpec("test", f"file://{root}", "master", None, "test")


def test_pinned_checkout_preserves_unchanged_tracked_mtimes_and_regenerates_feeds(tmp_path: Path):
    remote = tmp_path / "remote"
    spec = make_git(remote)
    manager = SourceManager(tmp_path / "sources")
    a = tmp_path / "a"
    tip = command("git", "rev-parse", "HEAD", cwd=remote)
    manager.checkout_pinned(spec, a, tip, None)
    old = a / "tools/llvm-bpf/Makefile"
    time = 1_700_000_000_444_555_666
    os.utime(old, ns=(time, time))
    old_feed = a / "feeds/packages/Makefile"
    old_feed.parent.mkdir(parents=True)
    old_feed.write_text("same feed")
    os.utime(old_feed, ns=(time, time))
    (remote / "rules.mk").write_text("changed")
    command("git", "commit", "-qam", "update common makefile", cwd=remote)
    b = tmp_path / "b"
    manager.checkout_pinned(spec, b, command("git", "rev-parse", "HEAD", cwd=remote), a)
    assert (b / "tools/llvm-bpf/Makefile").stat().st_mtime_ns == time
    assert (b / "rules.mk").read_text() == "changed"
    assert not (b / "feeds").exists()
    new_feed = b / "feeds/packages/Makefile"
    new_feed.parent.mkdir(parents=True)
    new_feed.write_text("same feed")
    retain_mtimes(a, b)
    assert new_feed.stat().st_mtime_ns == time
    assert (a / "rules.mk").read_text() == "common"


def test_device_sync_keeps_timestamp_of_identical_input_and_does_not_touch_compiler_dirs(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "Makefile").write_text("same")
    (target / "Makefile").write_text("same")
    (target / "obsolete").write_text("deleted upstream")
    (target / "build_dir").mkdir()
    (target / "build_dir/.built").touch()
    stamp = (target / "Makefile").stat().st_mtime_ns
    (source / "new").write_text("new")
    sync_source(source, target, preserve={"build_dir"})
    assert (target / "Makefile").stat().st_mtime_ns == stamp
    assert (target / "build_dir/.built").is_file()
    assert not (target / "obsolete").exists()
    assert (target / "new").read_text() == "new"


def test_older_writer_after_newer_and_check_then_save_race():
    revisions = {REPOSITORY: ("0", "1", "2"), UPSTREAM: ("a", "b", "c")}

    def ancestor(repo, base, head):
        history = revisions[repo]
        return base == head or (base in history and head in history and history.index(base) < history.index(head))

    old, new = Version("0", "a"), Version("1", "b")
    old_object, new_object = Candidate("old", old, "id"), Candidate("new", new, "id")
    assert has_newer([new_object], old, ancestor)
    assert not has_newer([], old, ancestor)  # A passed the initial inventory check.
    objects = {new_object.key: new_object}  # B won the publication race.
    objects.setdefault(old_object.key, old_object)
    assert select(list(objects.values()), Version("2", "c"), "id", ancestor) == new_object
    assert select(list(objects.values()), old, "id", ancestor) == old_object


def test_fork_and_incomparable_cache_versions_are_not_ordered():
    def ancestor(repo, base, head):
        return base == head or (base, head) in {("a", "b"), ("a", "c"), ("b", "c")}

    candidates = [
        Candidate("new-builder", Version("b", "a"), "host"),
        Candidate("new-upstream", Version("a", "b"), "host"),
        Candidate("fork", Version("x", "c"), "host"),
    ]
    assert select(candidates, Version("c", "c"), "host", ancestor) is None
    assert select(candidates[1:], Version("c", "c"), "host", ancestor) == candidates[1]
    assert select(candidates, Version("c", "c"), "toolchain", ancestor) is None


@pytest.mark.skipif(not shutil.which("zstd"), reason="cache transport requires zstd")
def test_archive_preserves_nanos_permissions_symlinks_and_rejects_escape(tmp_path: Path):
    source = tmp_path / "from"
    source.mkdir()
    file = source / "llvm"
    file.write_text("llvm")
    file.chmod(0o751)
    mtime = 1_700_000_000_111_222_333
    os.utime(file, ns=(mtime, mtime))
    (source / "clang").symlink_to("llvm")
    cache = tmp_path / "layer.zst"
    cache_archive.pack(cache, source, ["llvm", "clang"])
    restored = cache_archive.unpack(cache, tmp_path, lambda _: True, lambda _: None)
    assert (restored / "clang").readlink() == Path("llvm")
    assert (restored / "clang").read_text() == "llvm"
    assert (restored / "llvm").stat().st_mode & 0o777 == 0o751
    assert (restored / "llvm").stat().st_mtime_ns == mtime
    (source / "escape").symlink_to("../../outside")
    cache_archive.pack(cache, source, ["escape"])
    with pytest.raises(cache_archive.InvalidArchive, match="escaping link"):
        cache_archive.inspect(cache, lambda _: True)


@pytest.mark.skipif(not shutil.which("zstd"), reason="cache transport requires zstd")
def test_restore_drops_bad_manifest_without_promoting_any_files(tmp_path: Path):
    cache = ci.Cache(tmp_path)
    cache.transfer.mkdir(parents=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    ci.save(staging / "ci-manifest.json", {"ready": False})
    (staging / "llvm.tar.xz").write_text("blob")
    cache_archive.pack(cache.archive("dl"), staging, ["ci-manifest.json", "llvm.tar.xz"])
    version = Version("a" * 40, "b" * 40)
    plan = {"keys": {}, "version": asdict(version)}
    plan["keys"]["dl"] = f"{cache.prefix('dl', plan)}{version.builder}-{version.upstream}-{'c' * 64}"
    assert cache.restore("dl", plan, cache.dl, lambda *_: None) is None
    assert not cache.dl.exists()


def test_download_guard_checks_current_recipe_and_refetches_invalid_archive(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(repo / "scripts/owrt-cache-download.sh", tmp_path / "scripts/owrt-cache-download.sh")
    makeguard = (repo / "scripts/owrt-cache-download.mk").read_text()
    archive = tmp_path / "llvm.tar.xz"
    archive.write_text("real content")
    expected = ci.checksum(archive)
    (tmp_path / "Makefile").write_text(
        f"TOPDIR:={tmp_path}\nDL_DIR:={tmp_path}\nTMP_DIR:={tmp_path}/tmp\n"
        f"FILE:=llvm.tar.xz\nHASH:={expected}\nOWRT_RECHECK_CACHED_DOWNLOADS:=1\n"
        f"define Download\n{makeguard}download: $(DL_DIR)/$(FILE)\n"
        "$(DL_DIR)/$(FILE):\n\tprintf 'real content' > $$@\nendef\n$(eval $(call Download))\n"
    )
    assert "rechecking rejected" not in command("make", "download", cwd=tmp_path)
    archive.write_text("bad content")
    assert "rechecking rejected" in command("make", "download", cwd=tmp_path)
    assert ci.checksum(archive) == expected


def test_trusted_ref_requires_repo_real_branch_and_event(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/immortalwrt-master")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert ci.trusted()
    monkeypatch.setenv("GITHUB_REF", "refs/heads/immortalwrt-25.12.2")
    assert not ci.trusted()
    monkeypatch.setenv("GITHUB_REF", "refs/heads/immortalwrt-master")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert not ci.trusted()


def test_toolchain_rejects_mixed_environment_and_incomplete_install(tmp_path: Path):
    with pytest.raises(ValueError, match="workspace path changed"):
        ci.require_environment({"environment": "a"}, {"environment": "b"})
    (tmp_path / "build_dir/toolchain-arm64").mkdir(parents=True)
    (tmp_path / "staging_dir/toolchain-mips/bin").mkdir(parents=True)
    with pytest.raises(ValueError, match="matching build"):
        ci.require_toolchain(tmp_path)
    (tmp_path / "staging_dir/toolchain-arm64/bin").mkdir(parents=True)
    ci.require_toolchain(tmp_path)


def test_stage_requires_success_marker_and_failed_build_cannot_publish(tmp_path: Path):
    cache = ci.Cache(tmp_path)
    cache.transfer.mkdir(parents=True)
    ci.save(cache.plan_path, {})
    with pytest.raises(ValueError, match="compiled.json"):
        cache.stage("host")
    assert not list(cache.transfer.glob("*.tar.zst"))


def test_stale_publish_checks_both_authoritative_heads_and_newer_cache(tmp_path: Path, monkeypatch, capsys):
    builder, upstream = "b" * 40, "c" * 40
    current = Version(builder, upstream)
    latest = Version("d" * 40, "e" * 40)
    scope = ci.Cache(tmp_path)
    plan = {
        "version": asdict(current), "identities": {"host": "f" * 64},
        "upstream_url": "https://github.com/immortalwrt/immortalwrt.git",
    }
    ci.save(scope.plan_path, plan)
    scope.archive("host").write_bytes(b"data")
    monkeypatch.setattr(ci, "trusted", lambda: True)
    monkeypatch.setattr(
        ci, "git_head",
        lambda url, _: latest.upstream if UPSTREAM in url else latest.builder,
    )
    monkeypatch.setattr(Version, "precedes", lambda base, head, *_: base == head or (base, head) == (current, latest))
    monkeypatch.setattr(scope, "candidates", lambda *_: [Candidate("new", latest, "new")])
    scope.publish("host")
    assert "SKIP_STALE_PUBLISH" in capsys.readouterr().out

    monkeypatch.setattr(scope, "candidates", lambda *_: [])
    monkeypatch.setattr(ci, "github", lambda *_: {"active_caches_size_in_bytes": ci.budget()})
    scope.publish("host")
    assert "repository_cache_budget" in capsys.readouterr().out

    monkeypatch.setattr(ci, "github", lambda *_: {"active_caches_size_in_bytes": 0})
    scope.publish("host")
    assert f"PUBLISH_IMMUTABLE key={scope.prefix('host', plan)}{builder}-{upstream}" in capsys.readouterr().out

    monkeypatch.setattr(ci, "git_head", lambda *_: "a" * 40)
    scope.publish("host")
    assert "SKIP_STALE_PUBLISH" in capsys.readouterr().out


def test_warm_verification_requires_unchanged_actual_compiler_stamps(tmp_path: Path):
    cache = ci.Cache(tmp_path)
    ci.save(cache.plan_path, {"source_cached": True, "download_baseline": {"archive": [1, "sha"]}})
    llvm = "llvm-project-test/llvm-project-test/.built"
    toolchain = "toolchain-test/gcc-test/.built"
    result = {
        "host": {"reused": True}, "toolchain": {"reused": True},
        "make": {"before": {llvm: 1, toolchain: 1}, "changed": [], "commands": {"llvm_c_cpp": 0}},
    }
    ci.save(cache.transfer / "compiled.json", result)
    ci.verify_warm(cache)
    result["make"]["changed"] = [llvm]
    ci.save(cache.transfer / "compiled.json", result)
    with pytest.raises(ValueError, match="preserve all four"):
        ci.verify_warm(cache)
