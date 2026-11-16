"""Compile and execute the actual MediaTek Linux 6.18 PPE accounting patch functions."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/ppe-accounting"
PATCHES = ROOT / "fix_bugs/mediatek-6.18/patches"


def _function(path: Path, name: str, rename: str | None = None) -> str:
    source = path.read_text(encoding="utf-8")
    pattern = (
        r"(?m)^(?:static[ \t]+)?(?:int|bool|void|u32|struct[ \t]+\w+[ \t]*\*)"
        r"[ \t\n]+" + re.escape(name) + r"\("
    )
    match = re.search(pattern, source)
    assert match, name
    brace = source.index("{", match.end())
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    body = source[match.start():end]
    return body.replace(name + "(", (rename or name) + "(", 1)


def test_master_kernel_accounting_regressions(tmp_path: Path) -> None:
    compiler = shutil.which("cc")
    if not compiler:
        pytest.fail("a C compiler is required for kernel accounting regression checks")
    original = FIXTURES / "linux-6.18"
    patches = sorted(PATCHES.glob("*.patch"))
    assert len(patches) == 2

    patched = tmp_path / "linux"
    shutil.copytree(original, patched)
    subprocess.run(
        ["git", "apply", *(str(patch) for patch in patches)],
        cwd=patched,
        check=True,
    )
    driver = Path("drivers/net/ethernet/mediatek")
    replacements = {
        "ORIGINAL_UPDATE": _function(
            original / driver / "mtk_ppe.c",
            "mtk_flow_entry_update",
            "mtk_flow_entry_update_before",
        ),
        "FIXED_DETACH": _function(patched / driver / "mtk_ppe.c", "mtk_flow_entry_detach"),
        "FIXED_UPDATE": _function(
            patched / driver / "mtk_ppe.c",
            "mtk_flow_entry_update",
            "mtk_flow_entry_update_after",
        ),
        "ORIGINAL_STATS": _function(
            original / driver / "mtk_ppe_offload.c",
            "mtk_flow_offload_stats",
            "mtk_flow_offload_stats_before",
        ),
        "FIXED_STATS": _function(
            patched / driver / "mtk_ppe_offload.c",
            "mtk_flow_offload_stats",
            "mtk_flow_offload_stats_after",
        ),
        "ORIGINAL_DELETE": _function(
            original / "net/netfilter/nf_flow_table_offload.c",
            "flow_offload_work_del",
            "flow_offload_work_del_before",
        ),
        "FIXED_DELETE": _function(
            patched / "net/netfilter/nf_flow_table_offload.c",
            "flow_offload_work_del",
            "flow_offload_work_del_after",
        ),
    }
    source = (FIXTURES / "harness.c.in").read_text(encoding="utf-8")
    for marker, function in replacements.items():
        source = source.replace("@@" + marker + "@@", function)
    assert "@@" not in source
    c_file = tmp_path / "accounting.c"
    c_file.write_text(source, encoding="utf-8")
    binary = tmp_path / "accounting"
    subprocess.run(
        [compiler, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", str(c_file), "-o", str(binary)],
        check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True, check=True)
    assert "PASS:" in result.stdout
