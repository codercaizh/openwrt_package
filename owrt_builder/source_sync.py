"""Copy prepared source files only when their contents or mode changed."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


def same_file(left: Path, right: Path) -> bool:
    lstat, rstat = left.stat(), right.stat()
    if (lstat.st_size, stat.S_IMODE(lstat.st_mode)) != (rstat.st_size, stat.S_IMODE(rstat.st_mode)):
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while chunk := left_stream.read(1024 * 1024):
            if chunk != right_stream.read(1024 * 1024):
                return False
    return True


def sync_source(source: Path, destination: Path, *, preserve: set[str] | None = None) -> None:
    """Synchronize a builder-owned tree, leaving matching files and links untouched."""
    excluded = preserve or set()
    destination.mkdir(parents=True, exist_ok=True)
    originals = {entry.name: entry for entry in source.iterdir() if entry.name not in excluded}
    for entry in destination.iterdir():
        if entry.name in excluded or entry.name in originals:
            continue
        remove(entry)
    for name, entry in originals.items():
        target = destination / name
        if entry.is_symlink():
            if target.is_symlink() and target.readlink() == entry.readlink():
                continue
            remove(target)
            shutil.copy2(entry, target, follow_symlinks=False)
        elif entry.is_dir():
            if target.is_symlink() or not target.is_dir():
                remove(target)
            sync_source(entry, target)
        elif target.is_symlink() or not target.is_file() or not same_file(entry, target):
            remove(target)
            shutil.copy2(entry, target)


def retain_mtimes(previous: Path, current: Path) -> None:
    """Carry timestamps forward after checkout/feeds preparation verified equal bytes."""
    for path in current.iterdir():
        if path.name == ".git" or path.is_symlink():
            continue
        old = previous / path.name
        if old.is_symlink():
            continue
        if path.is_dir() and old.is_dir():
            retain_mtimes(old, path)
        elif path.is_file() and old.is_file() and same_file(old, path):
            times = old.stat()
            os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))


def remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)
