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
from owrt_builder.feeds import _git_commits
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
def test_archive_accepts_file_list_larger_than_linux_argv_limit(tmp_path: Path):
    source = tmp_path / "downloads"
    source.mkdir()
    first = source / "payload.tar.gz"
    first.write_bytes(b"payload")
    paths = ["payload.tar.gz"]
    for index in range(14_000):
        name = f"{index:05d}-{'x' * 174}.tar.gz"
        os.link(first, source / name)
        paths.append(name)
    assert sum(len(path) + 1 for path in paths) > 2 * 1024**2
    archive = tmp_path / "downloads.tar.zst"
    cache_archive.pack(archive, source, paths)
    assert cache_archive.inspect(archive, lambda _: True) == set(paths)


def test_compiler_restore_breaks_legacy_key_ties_only_within_same_version(tmp_path: Path, monkeypatch):
    cache = ci.Cache(tmp_path)
    version = Version("a" * 40, "b" * 40)
    plan = {"version": asdict(version), "identities": {"host": "new", "source": "new"}}
    a = Candidate("old-key-a", version, "old-a")
    b = Candidate("old-key-b", version, "old-b")
    monkeypatch.setattr(cache, "candidates", lambda *_: [b, a])
    assert cache.choose("host", plan, fallback=True) == a.key
    assert cache.choose("source", plan, fallback=True) == ""
    plan["identities"]["host"] = b.identity
    assert cache.choose("host", plan, fallback=True) == b.key


@pytest.mark.skipif(not shutil.which("zstd"), reason="cache transport requires zstd")
@pytest.mark.parametrize("change_toolchain_source", [False, True])
def test_restore_compiler_layers_from_old_manifest_uses_source_files(tmp_path: Path, change_toolchain_source: bool):
    cache = ci.Cache(tmp_path)
    version = Version("a" * 40, "b" * 40)
    identity = "c" * 64
    files = {"rules.mk": [0o644, "same content"]}
    plan = {
        "version": asdict(version), "keys": {}, "device": "n60", "target": "mediatek/filogic",
        "source_id": "mediatek", "snapshot": "snapshot", "host_files": files,
        "toolchain_files": {"rules.mk": [0o644, "changed"]} if change_toolchain_source else files,
    }
    paths = {
        "host": ["build_dir/host", "staging_dir/host"],
        "toolchain": ["build_dir/toolchain-example", "staging_dir/toolchain-example/bin"],
    }
    cache.transfer.mkdir(parents=True)
    for family, folders in paths.items():
        base = tmp_path / family
        for folder in folders:
            (base / folder).mkdir(parents=True)
            (base / folder / "artifact").write_text("built")
        ci.save(base / "ci-manifest.json", {
            "schema": ci.SCHEMA, "family": family, "repository": REPOSITORY, "branch": ci.BRANCH,
            "ready": True, "version": asdict(version), "identity": identity,
            "files": files, "mtimes": {"rules.mk": 1},
            "device": plan["device"], "target": plan["target"], "source_id": plan["source_id"],
            "environment": f"legacy-{family}", "inputs": "legacy-includes-environment",
        })
        cache_archive.pack(cache.archive(family), base, ["ci-manifest.json", "build_dir", "staging_dir"])
        plan["keys"][family] = f"{cache.prefix(family, plan)}{version.builder}-{version.upstream}-{identity}"

    ci.save(cache.plan_path, plan)
    cache.restore_compilers()
    assert cache.plan["host_restore"] is not None
    assert (cache.host / "build_dir/host/artifact").is_file()
    assert (cache.plan["toolchain_restore"] is None) == change_toolchain_source
    assert (cache.device / "openwrt/staging_dir/toolchain-example/bin/artifact").is_file() != change_toolchain_source


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


@pytest.mark.skipif(not shutil.which("zstd"), reason="cache transport requires zstd")
@pytest.mark.parametrize("changed_download", [False, True])
def test_download_archive_roundtrip_checks_files_but_excludes_transport_manifest(
    tmp_path: Path, changed_download: bool,
):
    cache = ci.Cache(tmp_path)
    staging = tmp_path / "downloads"
    staging.mkdir()
    package = staging / "linux.tar.xz"
    package.write_bytes(b"archive contents")
    (staging / "archive.partial").write_bytes(b"incomplete")
    version = Version("a" * 40, "b" * 40)
    identity = "c" * 64
    downloads = ci.download_entries(staging)
    assert set(downloads) == {"linux.tar.xz"}
    ci.save(staging / "ci-manifest.json", {
        "schema": ci.SCHEMA, "family": "dl", "repository": REPOSITORY, "branch": ci.BRANCH,
        "ready": True, "version": asdict(version), "identity": identity, "downloads": downloads,
    })
    if changed_download:
        package.write_bytes(b"changed bytes")
    cache_archive.pack(cache.archive("dl"), staging, ["ci-manifest.json", *downloads])
    key = f"{cache.prefix('dl', {})}{version.builder}-{version.upstream}-{identity}"
    metadata = cache.restore("dl", {"keys": {"dl": key}}, cache.dl, ci.verify_downloads)
    if changed_download:
        assert metadata is None
        assert not cache.dl.exists()
    else:
        assert metadata is not None
        assert (cache.dl / package.name).read_bytes() == package.read_bytes()
        assert list(cache.dl.iterdir()) == [cache.dl / package.name]


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


def test_toolchain_rejects_incomplete_install(tmp_path: Path):
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
    monkeypatch.setenv("GITHUB_SHA", builder)
    monkeypatch.setattr(
        ci, "git_head",
        lambda url, _: latest.upstream if UPSTREAM in url else latest.builder,
    )
    monkeypatch.setattr(Version, "precedes", lambda base, head, *_: base == head or (base, head) == (current, latest))
    monkeypatch.setattr(scope, "candidates", lambda *_: [Candidate("new", latest, "new")])
    scope.publish("host")
    assert "SKIP_STALE_PUBLISH" in capsys.readouterr().out

    monkeypatch.setattr(scope, "candidates", lambda *_: [])
    monkeypatch.setattr(ci, "inventory", lambda *_, **kwargs: (
        [{"size_in_bytes": ci.budget()}] if kwargs.get("all_refs") else []
    ))
    scope.publish("host")
    assert "repository_cache_budget" in capsys.readouterr().out

    monkeypatch.setattr(ci, "inventory", lambda *_, **__: [])
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


def test_publish_reclaims_confirmed_predecessor_before_counting_space(tmp_path, monkeypatch, capsys):
    cache = ci.Cache(tmp_path)
    old = Version("a" * 40, "b" * 40)
    new = Version("c" * 40, "b" * 40)
    source = cache.prefix("source", {"source_id": "armv8"})
    foreign_ref = "refs/heads/immortalwrt-25.12.2"
    toolchain = cache.prefix("toolchain", {"device": "s905d"})

    def row(cache_id, prefix, version, *, ref=ci.REF, size=2 * ci.MIB, identity="a"):
        return {"id": cache_id, "ref": ref, "size_in_bytes": size,
                "key": f"{prefix}{version.builder}-{version.upstream}-{identity * 64}"}

    rows = [
        row(1, source, old), row(2, source, new), row(3, source, old, ref=foreign_ref),
        row(4, cache.prefix("toolchain", {"device": "vplus"}), old),
        row(5, source, new, identity="b", size=0),
    ]
    monkeypatch.setenv("OWRT_CACHE_BUDGET_MIB", "8")
    monkeypatch.setenv("GITHUB_SHA", new.builder)
    monkeypatch.setattr(ci, "trusted", lambda: True)
    monkeypatch.setattr(ci, "git_head", lambda url, _: new.upstream if UPSTREAM in url else new.builder)
    monkeypatch.setattr(Version, "precedes", lambda a, b, *_: a == b or (a, b) == (old, new))
    monkeypatch.setattr(ci, "inventory", lambda prefix=None, *, all_refs=False: [
        x for x in rows if (all_refs or x["ref"] == ci.REF)
        and (prefix is None or x["key"].startswith(prefix))
    ])

    def delete(repo, path, *, method):
        assert (repo, method) == (REPOSITORY, "DELETE")
        assert path == "actions/caches/1"
        rows.pop(0)

    monkeypatch.setattr(ci, "github", delete)
    ci.save(cache.plan_path, {"version": asdict(new), "device": "s905d",
                              "identities": {"toolchain": "f" * 64},
                              "upstream_url": "https://github.com/immortalwrt/immortalwrt.git"})
    cache.archive("toolchain").write_bytes(bytes(ci.MIB))
    cache.publish("toolchain")
    log = capsys.readouterr().out
    assert log.index("decision=removed id=1") < log.index("decision=PUBLISH_IMMUTABLE")
    assert f"key={toolchain}{new.builder}-{new.upstream}" in log
    assert {entry["id"] for entry in rows} == {2, 3, 4, 5}


def test_generated_base_feed_alias_is_not_a_remote_pin(tmp_path: Path):
    source = tmp_path / "upstream"
    make_git(source)
    (source / "feeds").mkdir()
    (source / "feeds/base").symlink_to("..")
    native = source / "feeds/packages"
    make_git(native)
    expected = command("git", "rev-parse", "HEAD", cwd=native)
    assert _git_commits(source, (), runner=None) == {"default:packages": expected}


@pytest.mark.parametrize("keeper_present,newer_present,expected", [
    (True, False, [11, 13]), (False, False, []), (True, True, []),
])
def test_prune_removes_only_superseded_cache_ids(
    tmp_path: Path, monkeypatch, keeper_present: bool, newer_present: bool, expected: list[int],
):
    cache = ci.Cache(tmp_path)
    prefix = cache.prefix("toolchain", {"device": "netcore_n60-pro"})
    other_device = cache.prefix("toolchain", {"device": "360t7"})
    branch = ci.REF
    old = Version("a" * 40, "d" * 40)
    current = Version("b" * 40, "e" * 40)
    newer = Version("c" * 40, "f" * 40)
    fork = Version("c" * 40, "d" * 40)
    comparable = {(old, current), (old, newer), (current, newer)}
    monkeypatch.setattr(Version, "precedes", lambda a, b, *_: a == b or (a, b) in comparable)
    monkeypatch.setattr(ci, "git_head", lambda url, ref: newer.upstream if UPSTREAM in url else newer.builder)

    def row(cache_id, version, identity, *, ref=branch, scope=prefix):
        key = f"{scope}{version.builder}-{version.upstream}-{identity * 64}"
        return {"key": key, "id": cache_id, "ref": ref, "size_in_bytes": 100}

    keeper = row(12, current, "a")
    entries = [
        row(11, old, "b"),
        row(13, current, "c"),   # Existing variant at upload time.
        row(14, current, "d"),   # Arrived concurrently.
        row(15, fork, "e"),
        row(16, old, "f", ref="refs/heads/unrelated"),
        row(17, old, "1", scope=other_device),
    ]
    if keeper_present:
        entries.append(keeper)
    if newer_present:
        entries.append(row(18, newer, "b"))
    monkeypatch.setattr(ci, "inventory", lambda scope: entries if scope == prefix else [])
    deleted = []

    def delete(repo, path, *, method):
        assert (repo, method) == (REPOSITORY, "DELETE")
        deleted.append(int(path.rsplit("/", 1)[1]))

    monkeypatch.setattr(ci, "github", delete)
    cache.prune_publication({
        "family": "toolchain", "device": "n60pro", "key": keeper["key"], "id": keeper["id"],
        "same_version_ids": [13], "run": "99", "attempt": "2",
    }, run="99", attempt="2", builder=current.builder)
    assert deleted == expected


def test_prune_does_not_accept_publication_from_another_attempt(tmp_path: Path):
    cache = ci.Cache(tmp_path)
    with pytest.raises(ValueError, match="originating run"):
        cache.prune_publication({"family": "host", "run": "80", "attempt": "1"},
                                run="80", attempt="2", builder="b" * 40)


def test_audit_creates_a_prune_record_only_for_confirmed_upload(tmp_path: Path, monkeypatch):
    cache = ci.Cache(tmp_path)
    key = "compiler-cache-key"
    ci.save(cache.plan_path, {"device": "netcore_n60-pro"})
    ci.save(cache.transfer / "pending-host.json", {"key": key, "same_version_ids": [7]})
    monkeypatch.setenv("GITHUB_RUN_ID", "80")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setattr(ci, "sleep", lambda _: None)
    monkeypatch.setattr(ci, "inventory", lambda _: [])
    cache.audit("host", key)
    published = cache.transfer / "published" / "host.json"
    assert not published.exists()
    monkeypatch.setattr(ci, "inventory", lambda _: [{"id": 9, "key": key, "ref": ci.REF, "size_in_bytes": 100}])
    cache.audit("host", key)
    assert ci.load(published) == {
        "family": "host", "id": 9, "key": key, "same_version_ids": [7],
        "device": "netcore_n60-pro", "run": "80", "attempt": "2",
    }
