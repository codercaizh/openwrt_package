"""Small, defensive helpers for clearing generated workspace state.

The Web cache-cleanup action is deliberately destructive within two narrowly
defined workspace roots.  These helpers remove entries with ``lstat`` and
never traverse a symbolic link.  Keeping the implementation here avoids
duplicating subtly different cleanup rules between the compiler-cache and
source-cache managers.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import Any, Iterator


class CleanupError(RuntimeError):
    """A cleanup target or lock cannot be handled safely."""


def _absolute(path: str | os.PathLike[str]) -> Path:
    """Make a path absolute without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(path)))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CleanupError(f"无法检查清理目标: {path}: {exc}") from exc


def _assert_real_parent(path: Path) -> None:
    """Reject a target whose parent path would redirect outside its root."""

    parent = _absolute(path).parent
    # ``Path.lstat`` checks only the final component.  Walk every component
    # so ``workspace/link/cache`` cannot evade the guard by making only
    # ``workspace/link`` a symlink.
    current_path = Path(parent.anchor or os.sep)
    components = parent.parts[1:] if parent.anchor else parent.parts
    for component in components:
        current_path /= component
        current = _lstat(current_path)
        if current is None:
            continue
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise CleanupError(f"清理目标的父路径必须是真实目录: {path}")


def tree_size(path: str | os.PathLike[str]) -> int:
    """Return allocated bytes below *path* without following symlinks."""

    root = _absolute(path)
    _assert_real_parent(root)
    total = 0
    seen: set[tuple[int, int]] = set()
    pending = [root]
    while pending:
        item = pending.pop()
        item_stat = _lstat(item)
        if item_stat is None:
            continue
        inode = (int(getattr(item_stat, "st_dev", 0)), int(getattr(item_stat, "st_ino", 0)))
        if inode in seen:
            continue
        seen.add(inode)
        total += int(getattr(item_stat, "st_blocks", 0)) * 512 or int(item_stat.st_size)
        if not stat.S_ISDIR(item_stat.st_mode) or stat.S_ISLNK(item_stat.st_mode):
            continue
        try:
            with os.scandir(item) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CleanupError(f"无法读取清理目标: {item}: {exc}") from exc
    return total


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Remove one path using ``lstat``; a symlink is always only unlinked."""

    target = _absolute(path)
    item_stat = _lstat(target)
    if item_stat is None:
        return
    if not stat.S_ISDIR(item_stat.st_mode) or stat.S_ISLNK(item_stat.st_mode):
        try:
            target.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CleanupError(f"无法删除清理目标: {target}: {exc}") from exc
        return

    # Do not use ``Path.rglob`` or ``shutil.rmtree`` here: both make it easy
    # for a future refactor to accidentally follow a replaced symlink.  Each
    # recursive call starts with lstat and therefore has the same guarantee.
    try:
        with os.scandir(target) as entries:
            children = [Path(entry.path) for entry in entries]
    except FileNotFoundError:
        return
    except NotADirectoryError:
        return remove_tree(target)
    except OSError as exc:
        raise CleanupError(f"无法读取清理目录: {target}: {exc}") from exc
    for child in children:
        remove_tree(child)
    try:
        target.rmdir()
    except FileNotFoundError:
        return
    except NotADirectoryError:
        return remove_tree(target)
    except OSError as exc:
        # If a concurrent writer replaced the directory with a symlink, the
        # retry unlinks that symlink and still never enters its target.
        current = _lstat(target)
        if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode)):
            return remove_tree(target)
        raise CleanupError(f"无法删除清理目录: {target}: {exc}") from exc


def clear_directory(path: str | os.PathLike[str]) -> int:
    """Clear a generated root, recreate it, and return bytes released.

    A missing root is valid.  If the root itself is a symlink, only the link
    is removed; its target is never inspected or deleted.  Regular files,
    FIFOs and other abnormal root entries are unlinked in the same way.
    """

    root = _absolute(path)
    _assert_real_parent(root)
    released = tree_size(root)
    remove_tree(root)
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        _assert_real_parent(root)
        root.mkdir(parents=False, exist_ok=True)
    except FileExistsError:
        # A concurrent creator may have won after remove_tree.  Never accept
        # a symlink or an abnormal file as the resulting root.
        current = _lstat(root)
        if current is None or stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise CleanupError(f"清理根目录未恢复为真实目录: {root}")
    except OSError as exc:
        raise CleanupError(f"无法恢复清理根目录: {root}: {exc}") from exc
    current = _lstat(root)
    if current is None or stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise CleanupError(f"清理根目录未恢复为真实目录: {root}")
    return released


@contextmanager
def exclusive_lock(path: str | os.PathLike[str], *, timeout: float = 0.0) -> Iterator[Any]:
    """Acquire a process- and (on POSIX) cross-process exclusive lock."""

    lock_path = _absolute(path)
    _assert_real_parent(lock_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _lstat(lock_path)
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
            raise CleanupError(f"锁文件必须是普通文件: {lock_path}")
        handle = lock_path.open("a+")
    except CleanupError:
        raise
    except OSError as exc:
        raise CleanupError(f"无法打开清理锁: {lock_path}: {exc}") from exc

    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CleanupError(f"清理操作正在进行: {lock_path}")
                time.sleep(min(0.05, max(0.005, deadline - time.monotonic())))
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def workspace_cleanup_lock(workspace: str | os.PathLike[str], *, timeout: float = 0.0) -> Iterator[Any]:
    """Lock cleanup, source preparation and Web worker operations together."""

    with exclusive_lock(_absolute(workspace) / ".cache-cleanup.lock", timeout=timeout) as handle:
        yield handle


@contextmanager
def workspace_build_lock(workspace: str | os.PathLike[str], *, timeout: float = 0.0) -> Iterator[Any]:
    """Hold the normal BuildEngine lock while a cleanup is in progress."""

    with exclusive_lock(_absolute(workspace) / ".build.lock", timeout=timeout) as handle:
        yield handle


__all__ = [
    "CleanupError",
    "clear_directory",
    "exclusive_lock",
    "remove_tree",
    "tree_size",
    "workspace_build_lock",
    "workspace_cleanup_lock",
]
