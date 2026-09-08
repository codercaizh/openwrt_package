"""Deterministic feed preparation used while building a source snapshot.

The function in this module is synchronous by design.  A Web endpoint or
scheduled worker can run it in a background task and forward ``status`` events
without sharing an event loop with git or make.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Mapping, Sequence

from .sources import StatusCallback, _emit


class FeedError(RuntimeError):
    """A feed could not be prepared in the staging source."""


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str
    destination: str
    branch: str | None = None
    depth: int | None = 1


# These are the repositories used by the source snapshot preparer.  Passwall
# intentionally has no fixed commit.
FEED_SPECS: tuple[FeedSpec, ...] = (
    FeedSpec("kenzo", "https://github.com/kenzok8/openwrt-packages.git", "package/kenzo"),
    FeedSpec("rtp2httpd", "https://github.com/stackia/rtp2httpd.git", "package/rtp2httpd"),
    FeedSpec(
        "cloudflarespeedtest",
        "https://github.com/stevenjoezhang/luci-app-cloudflarespeedtest.git",
        "package/luci-app-cloudflarespeedtest",
    ),
    FeedSpec(
        "passwall-packages",
        "https://github.com/Openwrt-Passwall/openwrt-passwall-packages.git",
        "package/passwall-packages",
    ),
    FeedSpec(
        "passwall-luci",
        "https://github.com/Openwrt-Passwall/openwrt-passwall.git",
        "package/passwall-luci",
    ),
    FeedSpec(
        "golang",
        "https://github.com/sbwml/packages_lang_golang.git",
        "feeds/packages/lang/golang",
        branch="26.x",
    ),
)


PASSWALL_CORE_PACKAGES: tuple[str, ...] = (
    "xray-core",
    "v2ray-geodata",
    "sing-box",
    "chinadns-ng",
    "dns2socks",
    "hysteria",
    "ipt2socks",
    "microsocks",
    "naiveproxy",
    "shadowsocks-libev",
    "shadowsocks-rust",
    "shadowsocksr-libev",
    "simple-obfs",
    "tcping",
    "trojan-plus",
    "tuic-client",
    "v2ray-plugin",
    "xray-plugin",
    "geoview",
    "shadow-tls",
)

# These custom repositories are checked out below ``package/`` so that their
# package trees are visible to OpenWrt's package scanner.  The branch used by
# this project also contains older copies of some of these packages in the
# standard feeds.  ``scripts/feeds install`` creates symlinks for those copies;
# leaving the links in place makes package discovery order dependent and can
# silently select the upstream implementation instead of the requested
# replacement.
CUSTOM_PACKAGE_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "passwall-luci": ("luci-app-passwall",),
    "rtp2httpd": ("rtp2httpd", "luci-app-rtp2httpd"),
}


def _default_run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = True,
    text: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=capture_output,
        text=text,
        check=check,
    )


def _run_git(
    args: Sequence[str], *, cwd: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    run = runner or _default_run
    try:
        result = run(["git", *args], cwd=cwd)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise FeedError(f"git {' '.join(args)} failed: {detail}") from exc
    return (result.stdout or "").strip()


def _run_command(
    args: Sequence[str],
    *,
    cwd: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    run = runner or _default_run
    try:
        result = run(list(args), cwd=cwd)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise FeedError(f"{' '.join(args)} failed: {detail}") from exc
    return (result.stdout or "").strip()


def _safe_destination(root: Path, relative: str) -> Path:
    destination = (root / relative).resolve()
    root_resolved = root.resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise FeedError(f"feed destination escapes source: {relative}")
    return destination


def _clone_feed(
    source_root: Path,
    feed: FeedSpec,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    destination = _safe_destination(source_root, feed.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        else:
            shutil.rmtree(destination)
    args = ["clone", "--single-branch"]
    depth = feed.depth
    if depth:
        args.extend(["--depth", str(depth)])
    if feed.branch:
        args.extend(["--branch", feed.branch])
    args.extend([feed.url, str(destination)])
    _run_git(args, runner=runner)
    commit = _run_git(["rev-parse", "HEAD"], cwd=destination, runner=runner)
    if not commit or any(ch.isspace() for ch in commit):
        raise FeedError(f"feed {feed.name} did not produce a usable HEAD")
    return commit


def _patch_rust(source_root: Path, status: StatusCallback | None) -> None:
    makefile = source_root / "package/feeds/packages/rust/Makefile"
    if not makefile.exists():
        return
    content = makefile.read_text(encoding="utf-8", errors="replace")
    if "PKG_VERSION:=1.90.0" not in content:
        return
    fix = Path(__file__).resolve().parents[1] / "scripts/fix_bugs/rust_Makefile"
    if not fix.exists():
        raise FeedError(f"rust 1.90.0 requires missing patch: {fix}")
    replacement = fix.read_text(encoding="utf-8", errors="replace")
    if "PKG_VERSION" not in replacement:
        raise FeedError(f"rust patch has no package metadata: {fix}")
    makefile.write_text(replacement, encoding="utf-8")
    _emit(status, "feed", "applied verified rust 1.90.0 patch", feed="rust")


def _remove_core_passwall(source_root: Path, status: StatusCallback | None) -> None:
    net_dir = source_root / "feeds/packages/net"
    removed: list[str] = []
    for package in PASSWALL_CORE_PACKAGES:
        path = net_dir / package
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            removed.append(package)
    luci_passwall = source_root / "feeds/luci/applications/luci-app-passwall"
    if luci_passwall.exists() or luci_passwall.is_symlink():
        if luci_passwall.is_symlink() or luci_passwall.is_file():
            luci_passwall.unlink()
        else:
            shutil.rmtree(luci_passwall)
        removed.append("luci-app-passwall")
    if removed:
        _emit(status, "feed", "removed core passwall packages", removed=removed)


def _remove_custom_conflicts(source_root: Path, status: StatusCallback | None) -> None:
    """Remove installed feed links for packages supplied by custom repos.

    The removal is deliberately performed after ``feeds install``.  Removing
    the package from the feed tree before that command leaves stale entries in
    the generated feed indexes, while removing the links afterwards makes the
    package scanner see the checked-out custom tree deterministically.
    """

    removed: list[str] = []
    for feed_name, package_names in CUSTOM_PACKAGE_OVERRIDES.items():
        for package_name in package_names:
            for feed_dir in (source_root / "package/feeds/luci", source_root / "package/feeds/packages"):
                candidate = feed_dir / package_name
                if not (candidate.exists() or candidate.is_symlink()):
                    continue
                if candidate.is_symlink() or candidate.is_file():
                    candidate.unlink()
                else:
                    shutil.rmtree(candidate)
                removed.append(str(candidate.relative_to(source_root)))

    # Passwall's core packages are removed from ``feeds/packages/net`` before
    # install.  Older feed indexes can still leave dangling package/feeds
    # links behind; clear only dangling links and leave unrelated feeds alone.
    package_feeds = source_root / "package/feeds"
    if package_feeds.is_dir():
        for candidate in package_feeds.rglob("*"):
            if candidate.is_symlink() and not candidate.exists():
                candidate.unlink()
                removed.append(str(candidate.relative_to(source_root)))
    if removed:
        _emit(status, "feed", "replaced conflicting standard-feed packages", removed=removed)


def _native_feed_update(
    source_root: Path,
    *,
    status: StatusCallback | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> None:
    scripts_feeds = source_root / "scripts/feeds"
    if not scripts_feeds.is_file():
        raise FeedError(f"OpenWrt feeds script missing: {scripts_feeds}")
    _emit(status, "feeds", "updating default OpenWrt feeds")
    _run_command(["./scripts/feeds", "update", "-a"], cwd=source_root, runner=runner)


def _native_feed_install(
    source_root: Path,
    *,
    status: StatusCallback | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> None:
    _emit(status, "feeds", "installing OpenWrt feeds")
    _run_command(["./scripts/feeds", "install", "-a", "-f"], cwd=source_root, runner=runner)


def _native_package_metadata(
    source_root: Path,
    *,
    status: StatusCallback | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> None:
    """Run the OpenWrt metadata target and require both generated files."""

    _emit(status, "metadata", "generating native package metadata")
    # OpenWrt's toplevel makefile names this target prepare-tmpinfo; it runs
    # package-metadata.pl and produces both files consumed by scan_catalog.
    _run_command(["make", "prepare-tmpinfo"], cwd=source_root, runner=runner)
    missing = [
        str(path.relative_to(source_root))
        for path in (source_root / "tmp/.packageinfo", source_root / "tmp/.config-package.in")
        if not path.is_file()
    ]
    if missing:
        raise FeedError("native metadata target did not produce: " + ", ".join(missing))


def _git_commits(
    source_root: Path,
    feeds: Sequence[FeedSpec],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> dict[str, str]:
    """Record actual HEAD for every default and custom feed repository."""

    result: dict[str, str] = {}
    candidates: list[tuple[str, Path]] = []
    feeds_root = source_root / "feeds"
    if feeds_root.is_dir():
        for path in sorted(feeds_root.iterdir()):
            # scripts/feeds uses ``<feed>.tmp`` while updating indexes.  Those
            # transient worktrees are implementation details, not feeds that
            # belong in the immutable snapshot fingerprint.
            if path.name.startswith(".") or path.name.endswith(".tmp"):
                continue
            if path.is_dir() or path.is_symlink():
                candidates.append((f"default:{path.name}", path))
    for feed in feeds:
        path = _safe_destination(source_root, feed.destination)
        candidates.append((feed.name, path))
    seen: set[Path] = set()
    for name, path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.exists():
            continue
        seen.add(resolved)
        try:
            commit = _run_git(["rev-parse", "HEAD"], cwd=path, runner=runner)
        except FeedError:
            # A package tree can be a plain local directory rather than a git
            # repo.  It must not be represented as a fake commit.
            continue
        if commit:
            result[name] = commit
    return result


def prepare_feeds_sync(
    source_root: str | os.PathLike[str],
    *,
    status: StatusCallback | None = None,
    feed_specs: Sequence[FeedSpec] = FEED_SPECS,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    require_native: bool = True,
    require_metadata: bool = True,
) -> dict[str, str]:
    """Prepare all project feeds inside an un-published source staging tree.

    The caller is responsible for publishing the containing source snapshot.
    Therefore a failed clone or patch cannot affect the currently published
    source.  The return value records actual HEAD commits for the snapshot
    manifest.
    """

    root = Path(source_root).resolve()
    if not root.is_dir():
        raise FeedError(f"source root does not exist: {root}")
    commits: dict[str, str] = {}
    try:
        _emit(status, "feeds", "preparing project feeds", root=str(root))
        if require_native:
            # Keep this ordering aligned with OpenWrt's source workflow:
            # default feed indexes first, then project repos/patches, then
            # feed installation and metadata generation.
            _native_feed_update(root, status=status, runner=runner)
        for feed in feed_specs:
            _emit(status, "feed", f"cloning {feed.name}", feed=feed.name)
            _clone_feed(root, feed, runner=runner)
        _remove_core_passwall(root, status)
        _patch_rust(root, status)
        if require_native:
            _native_feed_install(root, status=status, runner=runner)
            _remove_custom_conflicts(root, status)
            if require_metadata:
                _native_package_metadata(root, status=status, runner=runner)
        commits = _git_commits(root, feed_specs, runner=runner)
        _emit(status, "feeds", "feeds prepared", feed_commits=commits)
        if require_metadata:
            missing = [
                str(path.relative_to(root))
                for path in (root / "tmp/.packageinfo", root / "tmp/.config-package.in")
                if not path.is_file()
            ]
            if missing:
                raise FeedError("authoritative package catalog unavailable: " + ", ".join(missing))
        return commits
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError(f"feed preparation failed: {exc}") from exc


__all__ = [
    "FEED_SPECS",
    "FeedError",
    "FeedSpec",
    "CUSTOM_PACKAGE_OVERRIDES",
    "PASSWALL_CORE_PACKAGES",
    "prepare_feeds_sync",
]
