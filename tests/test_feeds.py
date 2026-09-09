from __future__ import annotations

from pathlib import Path

import pytest

from owrt_builder.feeds import (
    FEED_SPECS,
    FeedError,
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
