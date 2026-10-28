from pathlib import Path

from owrt_builder import local_patches
from owrt_builder.sources import PREPARATION_VERSION


def test_old_237_kernel_overlay_is_disabled(tmp_path: Path) -> None:
    source = tmp_path / "official"
    source.mkdir()
    assert PREPARATION_VERSION >= 13
    for source_id in ("armv8", "immortalwrt-25.12-mediatek", "immortalwrt-mt798x"):
        assert local_patches.patch_fingerprint(source_id) == ""
        assert local_patches.install_local_patches(source, source_id) == ""
    assert list(source.iterdir()) == []


def test_removed_kernel_66_overlay_not_in_repo() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "scripts/iptv-hnat").exists()
    assert not (root / "scripts/fix_bugs/rust_Makefile").exists()
