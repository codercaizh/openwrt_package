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
    workflow = (ROOT / ".github/workflows/owrt-firmware.yml").read_text(encoding="utf-8")
    publisher = (ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8")
    assert "refs/heads/immortalwrt-master" in workflow
    assert "github.repository == 'codercaizh/openwrt_package'" in workflow
    assert "restore-keys:" not in workflow
    assert "uses: actions/cache@v4" not in workflow
    assert "contents: read" in workflow and "actions: read" in workflow
    assert "actions: write" not in workflow and "contents: write" not in workflow
    for layer in ("source", "host", "toolchain", "dl"):
        assert f".owrt/cache/transfer/{layer}.tar.zst" in workflow
        assert f"family: {layer}" in publisher
    assert "needs: build" in publisher
    download, publish = publisher.split("      - name: Publish device toolchain", 1)
    assert "merge-multiple: true" in download
    assert "id: check-bundle" in download and "transfer/plan.json" in download
    assert publish.count("steps.check-bundle.outcome == 'success'") == 3
    assert "group: owrt-master-cache-writes" in publisher and "queue: max" in publisher
    assert "actions: write" in publisher and "contents: write" not in publisher
    validation = (ROOT / ".github/workflows/config-review-build.yml").read_text()
    assert "needs: cache-cold" in validation
    assert validation.count("device: n60pro") == 2
    assert "needs.cache-cold.outputs.feed_pins" in validation
    catalog = load_catalog(ROOT / "configs/devices.toml")
    assert {catalog.source_for(device).branch for device in catalog.devices.values()} == {"master"}
