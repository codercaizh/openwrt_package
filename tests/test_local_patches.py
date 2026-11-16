from pathlib import Path
import shutil
import subprocess

import pytest

from owrt_builder import local_patches
from owrt_builder.catalog import Catalog, PackageMetadata
from owrt_builder.devices import SourceSpec
from owrt_builder.sources import SourceError, SourceManager


ROOT = Path(__file__).resolve().parents[1]
MASTER_SOURCE = "immortalwrt-master-mediatek"
BUNDLE = ROOT / "fix_bugs/mediatek-6.18"


def master_source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Makefile").write_text("include $(TOPDIR)/rules.mk\n", encoding="utf-8")
    target = root / "target/linux/mediatek"
    (target / "patches-6.18").mkdir(parents=True)
    (target / "Makefile").write_text("KERNEL_PATCHVER:=6.18\n", encoding="utf-8")
    return root


def test_master_installs_only_618_ppe_patches_and_is_idempotent(tmp_path: Path) -> None:
    source = master_source(tmp_path / "source with spaces")
    fingerprint = local_patches.install_local_patches(source, MASTER_SOURCE)
    first = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert fingerprint == local_patches.install_local_patches(source, MASTER_SOURCE)
    assert first == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    installed = source / "target/linux/mediatek/patches-6.18"
    expected = {p.name: p.read_bytes() for p in (BUNDLE / "patches").glob("*.patch")}
    assert {p.name: p.read_bytes() for p in installed.glob("*.patch")} == expected
    assert (source / local_patches.FIX_STAMP).read_text(encoding="utf-8").strip() == fingerprint
    assert not (source / "target/linux/generic").exists()

    (source / "target/linux/mediatek/Makefile").write_text("KERNEL_PATCHVER:=6.17\n", encoding="utf-8")
    with pytest.raises(local_patches.LocalPatchError, match="MediaTek Linux 6.18"):
        local_patches.install_local_patches(source, MASTER_SOURCE)


def test_arm_ramips_and_unknown_sources_receive_no_local_patch(tmp_path: Path) -> None:
    for source_id in ("armv8", "immortalwrt-master-ramips", "unknown"):
        assert local_patches.patch_fingerprint(source_id) == ""
        assert local_patches.install_local_patches(tmp_path, source_id) == ""
    assert list(tmp_path.iterdir()) == []


def test_patch_changes_invalidate_fingerprint_but_docs_do_not(tmp_path: Path, monkeypatch) -> None:
    fixes = tmp_path / "fix_bugs"
    shutil.copytree(ROOT / "fix_bugs", fixes)
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)
    first = local_patches.patch_fingerprint(MASTER_SOURCE)
    (fixes / "mediatek-6.18/README.md").write_text("documentation-only change\n", encoding="utf-8")
    assert local_patches.patch_fingerprint(MASTER_SOURCE) == first
    patch = next((fixes / "mediatek-6.18/patches").glob("990-01*.patch"))
    patch.write_text(patch.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    assert local_patches.patch_fingerprint(MASTER_SOURCE) != first


def test_missing_master_bundle_fails_preparation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(local_patches, "_fix_root", lambda: tmp_path / "missing")
    with pytest.raises(local_patches.LocalPatchError, match="no firmware fix scripts"):
        local_patches.patch_fingerprint(MASTER_SOURCE)


def test_numbered_scripts_run_in_order_and_stop_on_failure(tmp_path: Path, monkeypatch) -> None:
    fixes = tmp_path / "fix_bugs"
    bundle = fixes / "mediatek-6.18"
    bundle.mkdir(parents=True)
    (bundle / "020-fail.sh").write_text('echo "expected failure" >&2; exit 1\n', encoding="utf-8")
    (bundle / "010-first.sh").write_text('echo first > "$1/order"\n', encoding="utf-8")
    (bundle / "030-last.sh").write_text('echo last >> "$1/order"\n', encoding="utf-8")
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(local_patches.LocalPatchError, match="020-fail.sh.*expected failure"):
        local_patches.install_local_patches(source, MASTER_SOURCE)
    assert (source / "order").read_text(encoding="utf-8") == "first\n"
    assert not (source / local_patches.FIX_STAMP).exists()


def test_changed_fix_republishes_snapshot_and_failed_fix_keeps_previous(tmp_path: Path, monkeypatch) -> None:
    template = master_source(tmp_path / "template")
    fixes = tmp_path / "fix_bugs"
    shutil.copytree(ROOT / "fix_bugs", fixes)
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)

    def runner(args, **kwargs):
        if args[1:2] == ["clone"]:
            shutil.copytree(template, Path(args[-1]))
        elif args[1:3] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "same-source-sha\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    manager = SourceManager(
        tmp_path / "state",
        source_specs={
            MASTER_SOURCE: SourceSpec(
                MASTER_SOURCE,
                "https://example.invalid/official.git",
                "master",
                None,
                "mediatek",
            ),
        },
        command_runner=runner,
    )

    def prepare():
        return manager.prepare_source(
            MASTER_SOURCE,
            feed_preparer=lambda *_: {},
            catalog_builder=lambda root: Catalog(
                root=root,
                packages=[PackageMetadata(name="demo", title="Demo")],
                authoritative=True,
            ),
        )

    first = prepare()
    patch = next((fixes / "mediatek-6.18/patches").glob("990-01*.patch"))
    patch.write_text(patch.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    second = prepare()
    assert second.snapshot_id != first.snapshot_id
    current = tmp_path / "state" / MASTER_SOURCE / "current"
    previous_target = current.resolve()
    script = fixes / "mediatek-6.18/020-mediatek-ppe-accounting.sh"
    script.write_text("echo broken >&2; exit 1\n", encoding="utf-8")
    with pytest.raises(SourceError, match="firmware fix.*broken"):
        prepare()
    assert current.resolve() == previous_target
    assert second.path.is_dir() and second.catalog_path.is_file()


def test_active_branch_has_no_linux_612_patch_assets_or_fixtures() -> None:
    assert not (ROOT / "fix_bugs/010-fullcone-conntrack-events.sh").exists()
    assert not (ROOT / "fix_bugs/020-mediatek-ppe-accounting.sh").exists()
    assert not (ROOT / "fix_bugs/patches").exists()
    fixtures = ROOT / "tests/fixtures/ppe-accounting"
    assert not (fixtures / "linux").exists()
    assert not (fixtures / "baseline-patches").exists()
