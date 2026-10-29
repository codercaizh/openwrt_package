"""Apply reviewed, offline firmware fixes before publishing a source snapshot."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

from .paths import repository_root


FIX_STAMP = ".owrt-fix-bugs"
_SUPPORTED_SOURCES = frozenset({"armv8", "immortalwrt-25.12-mediatek"})


class LocalPatchError(RuntimeError):
    """A checked-in firmware fix is missing or incompatible with the source."""


def _fix_root() -> Path:
    return repository_root() / "fix_bugs"


def _scripts(root: Path) -> list[Path]:
    scripts = sorted(root.glob("[0-9][0-9][0-9]-*.sh"))
    if not scripts:
        raise LocalPatchError(f"no firmware fix scripts found in {root}")
    return scripts


def patch_fingerprint(source_id: str) -> str:
    if source_id not in _SUPPORTED_SOURCES:
        return ""
    root = _fix_root()
    _scripts(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LocalPatchError(f"firmware fix assets must not be symlinks: {path}")
        if not path.is_file() or path.suffix.lower() == ".md":
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n".encode())
    return digest.hexdigest()


def install_local_patches(source_root: Path, source_id: str) -> str:
    fingerprint = patch_fingerprint(source_id)
    if not fingerprint:
        return ""
    source_root = source_root.resolve(strict=True)
    for script in _scripts(_fix_root()):
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
