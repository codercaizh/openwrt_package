from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from owrt_builder.feeds import (
    CUSTOM_PACKAGE_OVERRIDES,
    FEED_SPECS,
    FeedSpec,
    FeedError,
    _clone_feed,
    _ensure_synced_feed_links,
    _remove_custom_conflicts,
    prepare_feeds_sync,
    validate_go_compatibility,
)


def _write_go_feed(root: Path, version: str = "1.27.0") -> None:
    major_minor, _, patch = version.partition(".")
    minor, _, patch = patch.partition(".")
    feed = root / "feeds/packages/lang/golang/golang/Makefile"
    feed.parent.mkdir(parents=True)
    feed.write_text(
        f"GO_VERSION_MAJOR_MINOR:={major_minor}.{minor}\n"
        f"GO_VERSION_PATCH:={patch}\n",
        encoding="utf-8",
    )


def _write_go_package(root: Path, relative: str, requirement: str) -> Path:
    package = root / relative
    package.mkdir(parents=True)
    (package / "Makefile").write_text(
        "PKG_BUILD_DEPENDS:=golang/host\nGO_PKG:=example.invalid/module\n",
        encoding="utf-8",
    )
    module = package / "go.mod"
    module.write_text(
        f"module example.invalid/module\n\ngo {requirement}\n",
        encoding="utf-8",
    )
    return module


def test_golang_feed_tracks_27_x() -> None:
    golang = next(feed for feed in FEED_SPECS if feed.name == "golang")
    assert golang.branch == "27.x"


def test_tailscale_feeds_follow_upstream_default_branches() -> None:
    tailscale = next(
        feed for feed in FEED_SPECS if feed.url == "https://github.com/openwrt/packages.git"
    )
    community = next(
        feed for feed in FEED_SPECS if feed.url == "https://github.com/openwrt/luci.git"
    )

    assert tailscale.url == "https://github.com/openwrt/packages.git"
    assert tailscale.destination == "feeds/packages/net/tailscale"
    assert tailscale.depth == 1
    assert tailscale.branch == "master"
    assert tailscale.source_subdir == "net/tailscale"
    assert tailscale.clone_destination == "staging/upstream/openwrt-packages"
    assert community.url == "https://github.com/openwrt/luci.git"
    assert community.destination == "feeds/luci/applications/luci-app-tailscale-community"
    assert community.depth == 1
    assert community.branch == "master"
    assert community.source_subdir == "applications/luci-app-tailscale-community"
    assert community.clone_destination == "staging/upstream/openwrt-luci"
    assert "commit" not in FeedSpec.__dataclass_fields__


def test_tailscale_override_removes_legacy_feed_links(tmp_path: Path) -> None:
    feed_root = tmp_path / "package/feeds"
    packages = feed_root / "packages"
    luci = feed_root / "luci"
    packages.mkdir(parents=True)
    luci.mkdir(parents=True)
    official_tailscale = tmp_path / "feeds/packages/net/tailscale"
    official_tailscale.mkdir(parents=True)
    (official_tailscale / "Makefile").write_text("official recipe\n", encoding="utf-8")
    official_community = tmp_path / "feeds/luci/applications/luci-app-tailscale-community"
    official_community.mkdir(parents=True)
    (official_community / "Makefile").write_text("official luci recipe\n", encoding="utf-8")
    legacy_target = tmp_path / "feed-targets/luci-app-tailscale"
    legacy_target.mkdir(parents=True)
    for feed_dir, name, target in (
        (packages, "tailscale", official_tailscale),
        (luci, "luci-app-tailscale", legacy_target),
        (luci, "luci-app-tailscale-community", official_community),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        (feed_dir / name).symlink_to(target)

    _remove_custom_conflicts(tmp_path, None)

    # Simulate a stale feed index that did not create the two newly synced
    # official package links; the post-install safety net must recreate them.
    (packages / "tailscale").unlink()
    (luci / "luci-app-tailscale-community").unlink()
    _ensure_synced_feed_links(
        tmp_path,
        [
            next(feed for feed in FEED_SPECS if feed.url == "https://github.com/openwrt/packages.git"),
            next(feed for feed in FEED_SPECS if feed.url == "https://github.com/openwrt/luci.git"),
        ],
    )

    assert (packages / "tailscale").is_symlink()
    assert (packages / "tailscale").resolve() == official_tailscale
    assert (luci / "luci-app-tailscale-community").is_symlink()
    assert (luci / "luci-app-tailscale-community").resolve() == official_community
    assert not (luci / "luci-app-tailscale").exists()
    assert not (luci / "luci-app-tailscale").is_symlink()
    assert CUSTOM_PACKAGE_OVERRIDES["tailscale"] == ("luci-app-tailscale",)


def test_sparse_feed_sync_copies_only_source_subdir_and_returns_repo_head(tmp_path: Path) -> None:
    source = tmp_path / "upstream"
    source.mkdir()
    (source / "net/tailscale/files").mkdir(parents=True)
    (source / "net/tailscale/Makefile").write_text("official\n", encoding="utf-8")
    (source / "net/tailscale/files/tailscaled.init").write_text("init\n", encoding="utf-8")
    (source / "other-package/Makefile").parent.mkdir(parents=True)
    (source / "other-package/Makefile").write_text("not selected\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", "-b", "master"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "upstream"], cwd=source, check=True)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()

    target = tmp_path / "source/feeds/packages/net/tailscale"
    spec = FeedSpec(
        "official-tailscale",
        str(source),
        "feeds/packages/net/tailscale",
        branch="master",
        depth=1,
        source_subdir="net/tailscale",
        clone_destination="staging/upstream/packages",
    )
    git_calls: list[list[str]] = []

    def recording_runner(args: list[str], *, cwd: Path | None = None):
        git_calls.append(args)
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    actual = _clone_feed(tmp_path / "source", spec, runner=recording_runner)

    assert actual == expected
    clone_args = next(args for args in git_calls if args[:2] == ["git", "clone"])
    assert "--depth" in clone_args
    assert "--filter=blob:none" in clone_args
    assert "--sparse" in clone_args
    assert ["git", "sparse-checkout", "set", "--no-cone", "net/tailscale"] in git_calls
    assert (target / "Makefile").read_text(encoding="utf-8") == "official\n"
    assert (target / "files/tailscaled.init").read_text(encoding="utf-8") == "init\n"
    assert not (target / "other-package").exists()
    assert not (tmp_path / "source/staging/upstream/packages/other-package").exists()

    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    prepared_commits = prepare_feeds_sync(
        prepared_root,
        feed_specs=[spec],
        require_native=False,
        require_metadata=False,
    )
    assert prepared_commits == {"official-tailscale": expected}
    assert (prepared_root / "feeds/packages/net/tailscale/Makefile").is_file()
    assert not (prepared_root / "staging/upstream/packages").exists()


def test_go_compatibility_checks_reviewed_modules_only(tmp_path: Path) -> None:
    _write_go_feed(tmp_path)
    module = _write_go_package(tmp_path, "package/passwall-packages/xray-core", "1.27")

    # These manifests must not affect the source compatibility decision.
    vendor = module.parent / "vendor" / "unrelated"
    vendor.mkdir(parents=True)
    (vendor / "go.mod").write_text("module vendor\n\ngo 1.99\n", encoding="utf-8")
    testdata = module.parent / "testdata"
    testdata.mkdir()
    (testdata / "go.mod").write_text("module testdata\n\ngo 1.99\n", encoding="utf-8")
    unrelated = tmp_path / "package/unrelated-tool"
    unrelated.mkdir(parents=True)
    (unrelated / "go.mod").write_text("module unrelated\n\ngo 1.99\n", encoding="utf-8")

    report = validate_go_compatibility(tmp_path)

    assert report.toolchain == (1, 27, 0)
    assert report.modules == {"package/passwall-packages/xray-core/go.mod": (1, 27, 0)}


def test_go_compatibility_rejects_module_newer_than_toolchain(tmp_path: Path) -> None:
    _write_go_feed(tmp_path, "1.27.0")
    _write_go_package(tmp_path, "package/passwall-packages/xray-core", "1.28")

    with pytest.raises(
        FeedError,
        match=r"Go toolchain 1\.27\.0 is incompatible: .*xray-core/go\.mod requires Go >= 1\.28\.0",
    ):
        validate_go_compatibility(tmp_path)


def test_go_module_requires_toolchain_metadata(tmp_path: Path) -> None:
    _write_go_package(tmp_path, "package/passwall-packages/xray-core", "1.27")

    with pytest.raises(FeedError, match="Go toolchain metadata is missing"):
        validate_go_compatibility(tmp_path)
