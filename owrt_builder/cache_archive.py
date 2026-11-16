"""Transport a single cache layer with nanosecond mtimes and checked paths."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
from typing import Callable


class InvalidArchive(ValueError):
    pass


def pack(archive: Path, root: Path, paths: list[str], *, exclude: list[str] | None = None) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["tar", "--format=pax", "--sort=name", "--pax-option=delete=atime,delete=ctime",
         "-I", "zstd -T2 -3", "-cf", str(archive), "-C", str(root),
         *(f"--exclude={name}" for name in exclude or ()), "--", *paths],
        check=True,
    )


def inspect(archive: Path, allowed: Callable[[str], bool], *, compiler: bool = False) -> set[str]:
    if not archive.is_file() or archive.is_symlink():
        raise InvalidArchive("missing regular archive")
    paths: dict[str, tarfile.TarInfo] = {}
    max_bytes = 22 * 1024**3
    total = 0
    with subprocess.Popen(["zstd", "-dc", "--", str(archive)], stdout=subprocess.PIPE) as stream:
        try:
            with tarfile.open(fileobj=stream.stdout, mode="r|") as tar:
                for entry in tar:
                    path = entry.name
                    parts = PurePosixPath(path).parts
                    if (
                        not parts or PurePosixPath(path).as_posix() != path
                        or path.startswith("/") or ".." in parts or path in paths
                        or not allowed(path) or len(paths) >= 1_000_000
                        or not (entry.isdir() or entry.isfile() or entry.issym() or entry.islnk())
                        or entry.mode & 0o6000
                    ):
                        raise InvalidArchive(f"unsafe archive member {path!r}")
                    total += entry.size
                    if total > max_bytes:
                        raise InvalidArchive("archive exceeds the expanded size limit")
                    paths[path] = entry
            # Consume trailing tar blocks so zstd checks the complete frame.
            while stream.stdout.read(1024 * 1024):
                pass
            if stream.wait():
                raise InvalidArchive("invalid zstd stream")
        finally:
            if stream.poll() is None:
                stream.kill()
    for path, entry in paths.items():
        for parent in PurePosixPath(path).parents:
            item = paths.get(parent.as_posix())
            if item is not None and not item.isdir():
                raise InvalidArchive(f"archive member traverses {parent}")
        if entry.islnk():
            target = paths.get(entry.linkname)
            if target is None or not target.isfile():
                raise InvalidArchive(f"unsafe hard link {path}")
        if entry.issym():
            if ".git" in PurePosixPath(path).parts or path == "ci-manifest.json":
                raise InvalidArchive(f"unsafe Git or manifest link {path}")
            link = entry.linkname
            if link.startswith("/"):
                resolved = posixpath.normpath(link)
                # OpenWrt prerequisites and wrappers use absolute paths inside
                # the worker. Source and download archives get no exceptions.
                prefixes = ("/usr/bin/", "/bin/", "/workspace/work/cache/host-tools/master/",
                            "/workspace/work/cache/builds/ci-device/openwrt/")
                if not compiler or not resolved.startswith(prefixes):
                    raise InvalidArchive(f"unsafe absolute link {path} -> {link}")
            else:
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
                if (resolved == ".." or resolved.startswith("../")
                    or (path.startswith("source/") and not resolved.startswith("source/") and resolved != "source")):
                    raise InvalidArchive(f"escaping link {path} -> {link}")
    return set(paths)


def unpack(
    archive: Path, workspace: Path, allowed: Callable[[str], bool],
    verify: Callable[[Path], None], *, compiler: bool = False,
) -> Path:
    """Validate and extract into a private directory before installing any path."""
    inspect(archive, allowed, compiler=compiler)
    root = Path(tempfile.mkdtemp(prefix="restore-", dir=workspace))
    try:
        subprocess.run(
            ["tar", "-I", "zstd", "-xf", str(archive), "--no-same-owner", "-C", str(root)],
            check=True,
        )
        verify(root)
        return root
    except BaseException:
        shutil.rmtree(root)
        raise


def install(root: Path, destination: Path, paths: list[str]) -> None:
    for relative in paths:
        target = destination / relative
        if target.exists() or target.is_symlink():
            raise InvalidArchive(f"cache installation would overwrite {target}")
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(root / relative, target)
