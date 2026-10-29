from pathlib import Path
import shutil
import subprocess

import pytest

from owrt_builder import local_patches
from owrt_builder.catalog import Catalog, PackageMetadata
from owrt_builder.devices import SourceSpec
from owrt_builder.sources import SourceError, SourceManager


ROOT = Path(__file__).resolve().parents[1]
PATCH_NAME = "952-net-conntrack-events-support-multiple-registrant.patch"
PATCH_PATH = Path("target/linux/generic/hack-6.12") / PATCH_NAME


def official_source(root: Path) -> Path:
    config = root / "target/linux/generic/config-6.12"
    config.parent.mkdir(parents=True)
    config.write_text("CONFIG_NET=y\n# CONFIG_NF_CONNTRACK_EVENTS is not set\n")
    makefile = root / "package/network/utils/fullconenat-nft/Makefile"
    makefile.parent.mkdir(parents=True)
    makefile.write_text("KCONFIG:=CONFIG_NF_CONNTRACK_EVENTS=y CONFIG_NF_CONNTRACK_CHAIN_EVENTS=y\n")
    mediatek = root / "target/linux/mediatek/Makefile"
    mediatek.parent.mkdir(parents=True)
    mediatek.write_text("KERNEL_PATCHVER:=6.12\n")
    shutil.copytree(
        ROOT / "tests/fixtures/ppe-accounting/baseline-patches",
        root / "target/linux/generic/pending-6.12",
    )
    return root


@pytest.mark.parametrize("source_id", ["armv8", "immortalwrt-25.12-mediatek"])
def test_official_fix_is_idempotent_and_fullcone_controls_enablement(tmp_path: Path, source_id: str) -> None:
    source = official_source(tmp_path / "source with spaces")
    fingerprint = local_patches.install_local_patches(source, source_id)
    first = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert fingerprint == local_patches.install_local_patches(source, source_id)
    assert first == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert (source / PATCH_PATH).read_bytes() == (ROOT / "fix_bugs/patches" / PATCH_NAME).read_bytes()
    config = (source / "target/linux/generic/config-6.12").read_text()
    assert config.count("# CONFIG_NF_CONNTRACK_CHAIN_EVENTS is not set") == 1
    assert "CONFIG_NF_CONNTRACK_CHAIN_EVENTS=y" not in config
    assert (source / local_patches.FIX_STAMP).read_text().strip() == fingerprint
    for patch in (ROOT / "fix_bugs/patches").glob("7*.patch"):
        assert (source / "target/linux/generic/pending-6.12" / patch.name).read_bytes() == patch.read_bytes()


def test_existing_different_upstream_patch_is_rejected_without_overwrite(tmp_path: Path) -> None:
    source = official_source(tmp_path / "source")
    patch = source / PATCH_PATH
    patch.parent.mkdir(parents=True)
    patch.write_text("different upstream implementation\n")
    config = source / "target/linux/generic/config-6.12"
    before = config.read_bytes()
    with pytest.raises(local_patches.LocalPatchError, match="existing kernel patch differs"):
        local_patches.install_local_patches(source, "armv8")
    assert patch.read_text() == "different upstream implementation\n"
    assert config.read_bytes() == before
    assert not (source / local_patches.FIX_STAMP).exists()


def test_incompatible_kernel_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "kernel66"
    source.mkdir()
    with pytest.raises(local_patches.LocalPatchError, match="Linux 6.12"):
        local_patches.install_local_patches(source, "immortalwrt-25.12-mediatek")
    assert list(source.iterdir()) == []


def test_unsupported_sources_do_not_receive_kernel_612_fix(tmp_path: Path) -> None:
    for source_id in ("test", "immortalwrt-mt798x"):
        assert local_patches.patch_fingerprint(source_id) == ""
        assert local_patches.install_local_patches(tmp_path, source_id) == ""
    assert list(tmp_path.iterdir()) == []


def test_script_and_patch_changes_invalidate_fingerprint_but_docs_do_not(tmp_path: Path, monkeypatch) -> None:
    fixes = tmp_path / "fix_bugs"
    shutil.copytree(ROOT / "fix_bugs", fixes)
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)
    first = local_patches.patch_fingerprint("armv8")
    (fixes / "README.md").write_text("documentation change\n")
    assert local_patches.patch_fingerprint("armv8") == first
    script = fixes / "010-fullcone-conntrack-events.sh"
    script.write_text(script.read_text() + "# script change\n")
    second = local_patches.patch_fingerprint("armv8")
    assert second != first
    patch = fixes / "patches" / PATCH_NAME
    patch.write_text(patch.read_text() + "tampering\n")
    assert local_patches.patch_fingerprint("armv8") != second
    source = official_source(tmp_path / "source")
    with pytest.raises(local_patches.LocalPatchError, match="checksum mismatch"):
        local_patches.install_local_patches(source, "armv8")
    assert not (source / PATCH_PATH).exists()


def test_missing_fix_bundle_fails_preparation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(local_patches, "_fix_root", lambda: tmp_path / "missing")
    with pytest.raises(local_patches.LocalPatchError, match="no firmware fix scripts"):
        local_patches.patch_fingerprint("armv8")


def test_numbered_scripts_run_in_order_and_stop_on_failure(tmp_path: Path, monkeypatch) -> None:
    fixes = tmp_path / "fix_bugs"
    fixes.mkdir()
    (fixes / "020-fail.sh").write_text('echo "expected failure" >&2; exit 1\n')
    (fixes / "010-first.sh").write_text('echo first > "$1/order"\n')
    (fixes / "030-last.sh").write_text('echo last >> "$1/order"\n')
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(local_patches.LocalPatchError, match="020-fail.sh.*expected failure"):
        local_patches.install_local_patches(source, "armv8")
    assert (source / "order").read_text() == "first\n"
    assert not (source / local_patches.FIX_STAMP).exists()


def test_changed_fix_republishes_snapshot_and_failed_fix_keeps_previous(tmp_path: Path, monkeypatch) -> None:
    template = official_source(tmp_path / "template")
    fixes = tmp_path / "fix_bugs"
    shutil.copytree(ROOT / "fix_bugs", fixes)
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)

    def runner(args, **kwargs):
        if args[1:2] == ["clone"]:
            shutil.copytree(template, Path(args[-1]))
        elif args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "same-source-sha\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager = SourceManager(tmp_path / "state", source_specs={
        "armv8": SourceSpec("armv8", "https://example.invalid/official.git", "v25.12.2", None, "arm"),
    }, command_runner=runner)

    def prepare():
        return manager.prepare_source("armv8", feed_preparer=lambda *_: {}, catalog_builder=lambda root: Catalog(
            root=root, packages=[PackageMetadata(name="demo", title="Demo")], authoritative=True,
        ))

    first = prepare()
    script = fixes / "010-fullcone-conntrack-events.sh"
    script.write_text(script.read_text() + "# changed fix\n")
    assert manager.current("armv8") is None
    second = prepare()
    assert second.snapshot_id != first.snapshot_id
    assert (second.path / PATCH_PATH).is_file()
    current = tmp_path / "state/armv8/current"
    previous_target = current.resolve()
    script.write_text("echo broken >&2; exit 1\n")
    with pytest.raises(SourceError, match="firmware fix.*broken"):
        prepare()
    assert current.resolve() == previous_target
    assert second.path.is_dir() and second.catalog_path.is_file()


def test_removed_kernel_66_overlay_not_in_repo() -> None:
    assert not (ROOT / "scripts/iptv-hnat").exists()
    assert not (ROOT / "scripts/fix_bugs/rust_Makefile").exists()
