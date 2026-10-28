"""Reviewed MT798x kernel fixes, included in source/cache provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from .paths import repository_root

SOURCE_ID = "immortalwrt-mt798x"


def bundle_root() -> Path:
    return repository_root() / "scripts" / "iptv-hnat"


def patch_fingerprint(source_id: str) -> str:
    if source_id != SOURCE_ID:
        return ""
    root = bundle_root()
    paths = [root / "reviewed.json", *sorted((root / "overlay").rglob("*"))]
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def install_local_patches(source_root: Path, source_id: str) -> str:
    """Fail early on upstream drift instead of discovering it two hours later."""
    fingerprint = patch_fingerprint(source_id)
    if not fingerprint:
        return ""
    root = bundle_root()
    reviewed = json.loads((root / "reviewed.json").read_text())
    for relative, expected in reviewed["sha256"].items():
        path = source_root / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"IPTV/HNAT patch needs upstream review: {relative}")
    for path in sorted((root / "overlay").rglob("*")):
        if not path.is_file():
            continue
        target = source_root / path.relative_to(root / "overlay")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o755 if target.name == "iptv-hnat-diag" else 0o644)
    # Generated provenance is deliberately outside the hashed overlay.
    (source_root / "files/etc").mkdir(parents=True, exist_ok=True)
    (source_root / "files/etc/iptv-hnat-diag.version").write_text(fingerprint + "\n")
    return fingerprint
