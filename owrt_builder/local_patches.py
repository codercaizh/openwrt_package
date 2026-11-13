"""Apply reviewed, offline firmware fixes before publishing a source snapshot."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from .paths import repository_root


FIX_STAMP = ".owrt-fix-bugs"
_SUPPORTED_SOURCES = frozenset({"armv8", "immortalwrt-25.12-mediatek"})
_MASTER_SOURCE = "immortalwrt-master-mediatek"
_MASTER_BUNDLE = "mediatek-6.18"


class LocalPatchError(RuntimeError):
    """A checked-in firmware fix is missing or incompatible with the source."""


def _fix_root() -> Path:
    return repository_root() / "fix_bugs"


def _bundle(source_id: str) -> Path | None:
    if source_id == _MASTER_SOURCE:
        return _fix_root() / _MASTER_BUNDLE
    if source_id in _SUPPORTED_SOURCES:
        return _fix_root()
    return None


def _scripts(root: Path) -> list[Path]:
    scripts = sorted(root.glob("[0-9][0-9][0-9]-*.sh"))
    if not scripts:
        raise LocalPatchError(f"no firmware fix scripts found in {root}")
    return scripts


def patch_fingerprint(source_id: str) -> str:
    root = _bundle(source_id)
    if root is None:
        return ""
    if root.is_symlink():
        raise LocalPatchError(f"firmware fix assets must not be symlinks: {root}")
    _scripts(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == _MASTER_BUNDLE:
            continue
        if path.is_symlink():
            raise LocalPatchError(f"firmware fix assets must not be symlinks: {path}")
        if not path.is_file() or path.suffix.lower() == ".md":
            continue
        digest.update(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative.as_posix()}\n".encode())
    return digest.hexdigest()


def install_local_patches(source_root: Path, source_id: str) -> str:
    root = _bundle(source_id)
    if root is None:
        return ""
    fingerprint = patch_fingerprint(source_id)
    source_root = source_root.resolve(strict=True)
    for script in _scripts(root):
        result = subprocess.run(
            ["sh", str(script), str(source_root)],
            cwd=source_root,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise LocalPatchError(f"firmware fix {script.name} failed: {detail}")
    (source_root / FIX_STAMP).write_text(f"{fingerprint}\n", encoding="utf-8")
    return fingerprint
