from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from owrt_builder.build import BuildEngine, BuildError, BuildRequest
from owrt_builder.sources import PREPARATION_VERSION


def _capture_worker_command(tmp_path: Path, device: str) -> list[str]:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path / device)
    commands: list[list[str]] = []
    engine._check_docker = lambda: None  # type: ignore[method-assign]
    engine._image_exists = lambda _image: True  # type: ignore[method-assign]

    def run_stream(command, **_kwargs):
        commands.append(list(command))
        return 1

    engine._run_stream = run_stream  # type: ignore[method-assign]
    result = engine.build(BuildRequest(device=device, task_id=f"command-{device}"))
    assert not result.ok
    assert commands
    return commands[0]


def test_mediatek_worker_keeps_host_uid_without_privilege_or_host_network(tmp_path: Path) -> None:
    command = _capture_worker_command(tmp_path, "n60pro")

    assert "--privileged" not in command
    assert "--network=host" not in command
    assert "--network" not in command
    assert "--user" in command


def test_arm_worker_uses_privilege_without_host_uid_or_host_network(tmp_path: Path) -> None:
    command = _capture_worker_command(tmp_path, "s905d")

    assert "--privileged" in command
    assert "--network=host" not in command
    assert "--network" not in command
    assert "--user" not in command


def test_cancel_removes_deterministic_worker_container(monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(list(command))

    monkeypatch.setattr("owrt_builder.build.subprocess.run", run)
    BuildEngine.cancel(BuildEngine.__new__(BuildEngine), "cancel-container")

    assert commands == [["docker", "rm", "-f", "owrt-build-cancel-container"]]


def test_sysupgrade_metadata_can_be_nested_under_profile_directory(tmp_path: Path) -> None:
    image = tmp_path / "sysupgrade.bin"
    metadata = b"supported_devices=netcore,n60-pro\n"
    with tarfile.open(image, "w") as archive:
        info = tarfile.TarInfo("sysupgrade-netcore_n60-pro/CONTROL/metadata")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))

    assert "netcore,n60-pro" in BuildEngine._tar_metadata(image)


def test_sysupgrade_uses_target_fwtool_metadata(tmp_path: Path) -> None:
    image = tmp_path / "sysupgrade.bin"
    image.write_bytes(b"firmware")
    fwtool = tmp_path / "openwrt" / "staging_dir" / "host" / "bin" / "fwtool"
    fwtool.parent.mkdir(parents=True)
    fwtool.write_text(
        "#!/bin/sh\nprintf '%s\\n' '{\"supported_devices\":[\"netcore,n60-pro\"]}'\n",
        encoding="utf-8",
    )
    fwtool.chmod(0o755)

    assert BuildEngine._firmware_supported_devices(image, tmp_path / "openwrt") == ["netcore,n60-pro"]


def test_pipeline_applies_jobs_only_to_final_compile(tmp_path: Path) -> None:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path, use_docker=False)
    openwrt = tmp_path / "openwrt"
    openwrt.mkdir()
    config = tmp_path / "generated.config"
    config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
    commands: list[list[str]] = []

    def run_checked(command, **_kwargs):
        commands.append(list(command))

    engine._run_checked = run_checked  # type: ignore[method-assign]
    engine._run_pipeline(
        SimpleNamespace(key="n60pro", profile="netcore_n60-pro"),
        SimpleNamespace(snapshot_id="snapshot"),
        openwrt,
        config,
        lambda _line: None,
        None,
        jobs=3,
    )

    assert commands == [["make", "defconfig"], ["make", "download"], ["make", "-j3"]]


def test_requested_snapshot_errors_preserve_missing_vs_contaminated_reason(tmp_path: Path) -> None:
    """Worker diagnostics identify a missing snapshot separately from pollution."""

    workspace = tmp_path / "workspace"
    source_id = "immortalwrt-mt798x"
    snapshot_id = "contaminated-snapshot"
    snapshot = workspace / "sources" / source_id / "snapshots" / snapshot_id
    source = snapshot / "source"
    source.mkdir(parents=True)
    (source / "staging_dir").mkdir()
    (snapshot / "catalog.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "manifest.json").write_text(
        json.dumps({
            "preparation_version": PREPARATION_VERSION,
            "source_id": source_id,
            "snapshot_id": snapshot_id,
            "source_commit": "source-sha",
        }) + "\n",
        encoding="utf-8",
    )
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=workspace, use_docker=False)
    spec = engine.catalog.resolve("n60pro")

    with pytest.raises(BuildError, match=r"source snapshot invalid: .*staging_dir"):
        engine._prepare_snapshot(spec, snapshot_id, workspace, lambda _line: None, None)
    with pytest.raises(BuildError, match=r"source snapshot missing:"):
        engine._prepare_snapshot(spec, "missing-snapshot", workspace, lambda _line: None, None)
