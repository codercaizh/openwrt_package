from __future__ import annotations

from pathlib import Path

from owrt_builder.devices import load_catalog


ROOT = Path(__file__).resolve().parents[1]


def test_daily_build_matrix_covers_all_supported_devices() -> None:
    workflow = (ROOT / ".github/workflows/daily-build.yml").read_text(encoding="utf-8")
    assert '    - cron: "0 15 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "fail-fast: false" in workflow
    assert "max-parallel: 10" in workflow
    assert "uses: ./.github/workflows/build.yml" in workflow

    matrix_lines = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("device: [")
    ]
    assert len(matrix_lines) == 1
    matrix_devices = [
        name.strip() for name in matrix_lines[0].split("[", 1)[1].split("]", 1)[0].split(",")
    ]
    catalog = load_catalog(ROOT / "configs/devices.toml")
    resolved = [catalog.resolve(device).key for device in matrix_devices]
    assert len(resolved) == len(set(resolved)), "matrix must not duplicate a device"
    assert set(resolved) == set(catalog.devices), "matrix must cover every supported device"


def test_build_workflow_shares_host_tools_and_keeps_target_toolchains_device_scoped() -> None:
    workflow = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    assert ".owrt/cache/host-tools/${{ steps.cache_scope.outputs.host_scope }}/build_dir/host" in workflow
    assert "owrt-host-v2-${{ runner.os }}-${{ steps.cache_scope.outputs.host_scope }}" in workflow
    assert "owrt-toolchain-v2-${{ runner.os }}-${{ steps.cache_scope.outputs.host_scope }}-${{ steps.cache_scope.outputs.canonical }}" in workflow
    assert ".owrt/cache/dl/llvm-project-${{ steps.cache_scope.outputs.llvm_version }}.src.tar.xz" in workflow
    assert "github.ref_name" not in workflow
    assert ".owrt/cache/dl\n" not in workflow
