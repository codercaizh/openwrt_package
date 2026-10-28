"""Local patches are intentionally disabled for the official ImmortalWrt v25.12.2 build.

The former 237/6.6 IPTV/HNAT overlay MUST NOT be applied to kernel 6.12.
"""

from __future__ import annotations
from pathlib import Path


def patch_fingerprint(source_id: str) -> str:
    return ""


def install_local_patches(source_root: Path, source_id: str) -> str:
    return ""
