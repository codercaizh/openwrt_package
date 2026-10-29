"""Regression checks against the actual backported kernel functions."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from owrt_builder import local_patches


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/ppe-accounting"
PATCHES = ROOT / "fix_bugs/patches"
PPE_PATCH = "736-06-net-ethernet-mediatek-preserve-ppe-flow-accounting.patch"
FINAL_PATCH = "708-netfilter-flowtable-fetch-final-hardware-counters.patch"
SCRIPT = ROOT / "fix_bugs/020-mediatek-ppe-accounting.sh"


def _source(root: Path) -> Path:
    pending = root / "target/linux/generic/pending-6.12"
    shutil.copytree(FIXTURES / "baseline-patches", pending)
    (pending.parent / "config-6.12").write_text("# official kernel config\n")
    makefile = root / "target/linux/mediatek/Makefile"
    makefile.parent.mkdir(parents=True)
    makefile.write_text("KERNEL_PATCHVER:=6.12\n")
    return root


def _run(source: Path, script: Path = SCRIPT) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(script), str(source)], capture_output=True, text=True)


@pytest.mark.parametrize("name", ["736-03", "736-04", "736-05"])
def test_changed_baseline_is_rejected_before_installing_patches(tmp_path: Path, name: str) -> None:
    source = _source(tmp_path / "source")
    pending = source / "target/linux/generic/pending-6.12"
    baseline = next(pending.glob(name + "*.patch"))
    baseline.write_text(baseline.read_text() + "# changed upstream\n")
    result = _run(source)
    assert result.returncode != 0 and "checksum mismatch" in result.stderr
    assert not (pending / PPE_PATCH).exists()
    assert not (pending / FINAL_PATCH).exists()


def test_newer_kernel_requires_reviewed_backport(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    (source / "target/linux/mediatek/Makefile").write_text("KERNEL_PATCHVER:=6.18\n")
    result = _run(source)
    assert result.returncode != 0 and "MediaTek Linux 6.12" in result.stderr


def test_conflicting_second_patch_does_not_install_first_patch(tmp_path: Path) -> None:
    source = _source(tmp_path / "source")
    pending = source / "target/linux/generic/pending-6.12"
    (pending / PPE_PATCH).write_text("different upstream fix\n")
    result = _run(source)
    assert result.returncode != 0 and "existing kernel patch differs" in result.stderr
    assert not (pending / FINAL_PATCH).exists()
    assert (pending / PPE_PATCH).read_text() == "different upstream fix\n"


def test_tampered_fix_asset_changes_fingerprint_and_is_rejected(tmp_path: Path, monkeypatch) -> None:
    fixes = tmp_path / "fix_bugs"
    shutil.copytree(ROOT / "fix_bugs", fixes)
    monkeypatch.setattr(local_patches, "_fix_root", lambda: fixes)
    fingerprint = local_patches.patch_fingerprint("immortalwrt-25.12-mediatek")
    asset = fixes / "patches" / PPE_PATCH
    asset.write_text(asset.read_text() + "tampering\n")
    assert local_patches.patch_fingerprint("immortalwrt-25.12-mediatek") != fingerprint
    result = _run(_source(tmp_path / "source"), fixes / SCRIPT.name)
    assert result.returncode != 0 and "checksum mismatch" in result.stderr


def _function(path: Path, name: str, rename: str | None = None) -> str:
    source = path.read_text()
    pattern = (r"(?m)^(?:static[ \t]+)?(?:int|bool|void|u32|struct[ \t]+\w+[ \t]*\*)"
               r"[ \t\n]+" + re.escape(name) + r"\(")
    match = re.search(pattern, source)
    assert match, name
    brace = source.index("{", match.end())
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    body = source[match.start():end]
    return body.replace(name + "(", (rename or name) + "(", 1)


def test_kernel_accounting_regressions(tmp_path: Path) -> None:
    compiler = shutil.which("cc")
    if not compiler:
        pytest.fail("a C compiler is required for kernel accounting regression checks")
    original = FIXTURES / "linux"
    patched = tmp_path / "linux"
    shutil.copytree(original, patched)
    subprocess.run(["git", "apply", str(PATCHES / FINAL_PATCH), str(PATCHES / PPE_PATCH)], cwd=patched, check=True)
    driver = Path("drivers/net/ethernet/mediatek")
    replacements = {
        "ORIGINAL_UPDATE": _function(original / driver / "mtk_ppe.c", "mtk_flow_entry_update", "mtk_flow_entry_update_before"),
        "FIXED_DETACH": _function(patched / driver / "mtk_ppe.c", "mtk_flow_entry_detach"),
        "FIXED_UPDATE": _function(patched / driver / "mtk_ppe.c", "mtk_flow_entry_update", "mtk_flow_entry_update_after"),
        "ORIGINAL_STATS": _function(original / driver / "mtk_ppe_offload.c", "mtk_flow_offload_stats", "mtk_flow_offload_stats_before"),
        "FIXED_STATS": _function(patched / driver / "mtk_ppe_offload.c", "mtk_flow_offload_stats", "mtk_flow_offload_stats_after"),
        "ORIGINAL_DELETE": _function(original / "net/netfilter/nf_flow_table_offload.c", "flow_offload_work_del", "flow_offload_work_del_before"),
        "FIXED_DELETE": _function(patched / "net/netfilter/nf_flow_table_offload.c", "flow_offload_work_del", "flow_offload_work_del_after"),
    }
    source = (FIXTURES / "harness.c.in").read_text()
    for marker, function in replacements.items():
        source = source.replace("@@" + marker + "@@", function)
    assert "@@" not in source
    c_file = tmp_path / "accounting.c"
    c_file.write_text(source)
    binary = tmp_path / "accounting"
    subprocess.run([compiler, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", str(c_file), "-o", str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True, check=True)
    assert "PASS:" in result.stdout
