from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from owrt_builder.build import BuildCancelled, BuildEngine, BuildError, BuildRequest, CommandFailed
from owrt_builder.catalog import Catalog, KconfigOption, PackageMetadata
from owrt_builder.devices import DeviceCatalog, DeviceSpec, SourceSpec
from owrt_builder.sources import PREPARATION_VERSION, SourceManager


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


def _git_seed_source(root: Path) -> Path:
    source = root / "source"
    seed = source / "dl" / "datconf-6bb733f7.tar.bz2"
    seed.parent.mkdir(parents=True)
    seed.write_bytes(b"trusted source seed\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "add", "dl"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "seed"], check=True)
    SourceManager._clean_generated_tree(source)
    return source


def test_mediatek_worker_keeps_host_uid_without_privilege_or_host_network(tmp_path: Path) -> None:
    command = _capture_worker_command(tmp_path, "n60pro")

    assert "--privileged" not in command
    assert "--network=host" not in command
    assert "--network" not in command
    assert "--user" in command


def test_worker_disables_python_output_buffering_for_live_logs(tmp_path: Path) -> None:
    command = _capture_worker_command(tmp_path, "n60pro")

    env_index = command.index("--env")
    env_values = {
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--env" and index + 1 < len(command)
    }
    assert "PYTHONUNBUFFERED=1" in env_values
    assert command[env_index + 1] == "OWRT_BUILDER_WORKER=1"


def test_stream_forwards_low_volume_output_before_process_exit(tmp_path: Path) -> None:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path, use_docker=False)
    log_path = tmp_path / "docker.log"
    observed: list[float] = []
    log_at_first_line: list[bytes] = []
    started = time.monotonic()

    def callback(line: str) -> None:
        if line == "first-line":
            observed.append(time.monotonic() - started)
            log_at_first_line.append(log_path.read_bytes())

    command = [
        sys.executable,
        "-c",
        "import sys, time; print('first-line', flush=True); time.sleep(1.2); print('last-line', flush=True)",
    ]
    assert engine._run_stream(
        command,
        cwd=tmp_path,
        callback=callback,
        cancel_event=None,
        log_path=log_path,
    ) == 0

    assert observed and observed[0] < 0.8
    assert log_at_first_line and b"first-line\n" in log_at_first_line[0]
    assert log_path.read_bytes().endswith(b"last-line\n")


def test_copy_snapshot_stages_tracked_download_seed_before_linking_public_cache(tmp_path: Path) -> None:
    source = _git_seed_source(tmp_path)
    workspace = tmp_path / "workspace"
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    destination = BuildEngine._copy_snapshot(
        source,
        build_dir,
        lambda _line: None,
        workspace=workspace,
    )

    cached_seed = workspace / "cache" / "dl" / "datconf-6bb733f7.tar.bz2"
    assert cached_seed.read_bytes() == b"trusted source seed\n"
    assert (destination / "dl").is_symlink()
    assert (destination / "dl" / "datconf-6bb733f7.tar.bz2").read_bytes() == cached_seed.read_bytes()


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


def test_pipeline_retries_failed_formal_compile_with_verbose_output(tmp_path: Path) -> None:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path, use_docker=False)
    openwrt = tmp_path / "openwrt"
    openwrt.mkdir()
    config = tmp_path / "generated.config"
    config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
    commands: list[list[str]] = []
    lines: list[str] = []

    def run_checked(command, **_kwargs):
        commands.append(list(command))
        if command == ["make", "-j3"]:
            raise CommandFailed(command, 2)

    engine._run_checked = run_checked  # type: ignore[method-assign]
    engine._run_pipeline(
        SimpleNamespace(key="n60pro", profile="netcore_n60-pro"),
        SimpleNamespace(snapshot_id="snapshot"),
        openwrt,
        config,
        lines.append,
        None,
        jobs=3,
    )

    assert commands == [
        ["make", "defconfig"],
        ["make", "download"],
        ["make", "-j3"],
        ["make", "V=s", "-j1"],
    ]
    assert any("开始串行详细诊断：make V=s -j1" in line for line in lines)
    assert any("首次正式编译错误" in line for line in lines)
    assert any("串行详细诊断编译成功，继续后续打包" in line for line in lines)


def test_pipeline_preserves_formal_error_when_verbose_compile_fails(tmp_path: Path) -> None:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path, use_docker=False)
    openwrt = tmp_path / "openwrt"
    openwrt.mkdir()
    config = tmp_path / "generated.config"
    config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
    commands: list[list[str]] = []
    lines: list[str] = []

    def run_checked(command, **_kwargs):
        commands.append(list(command))
        if command == ["make", "-j2"]:
            raise CommandFailed(command, 2)
        if command == ["make", "V=s", "-j1"]:
            raise CommandFailed(command, 7)

    engine._run_checked = run_checked  # type: ignore[method-assign]
    with pytest.raises(CommandFailed, match=r"command exited 7: make V=s -j1$"):
        engine._run_pipeline(
            SimpleNamespace(key="n60pro", profile="netcore_n60-pro"),
            SimpleNamespace(snapshot_id="snapshot"),
            openwrt,
            config,
            lines.append,
            None,
            jobs=2,
        )

    assert commands[-2:] == [["make", "-j2"], ["make", "V=s", "-j1"]]
    assert any("首次正式编译错误" in line and "exited 2" in line for line in lines)
    assert any("串行详细诊断编译失败" in line and "exited 7" in line for line in lines)


def test_pipeline_does_not_start_verbose_compile_after_cancellation(tmp_path: Path) -> None:
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path, use_docker=False)
    openwrt = tmp_path / "openwrt"
    openwrt.mkdir()
    config = tmp_path / "generated.config"
    config.write_text("CONFIG_TEST=y\n", encoding="utf-8")
    commands: list[list[str]] = []
    lines: list[str] = []

    class CancelEvent:
        def __init__(self) -> None:
            self.cancelled = False

        def is_set(self) -> bool:
            return self.cancelled

    cancel_event = CancelEvent()

    def run_checked(command, **_kwargs):
        commands.append(list(command))
        if command == ["make", "-j4"]:
            cancel_event.cancelled = True
            raise CommandFailed(command, 2)

    engine._run_checked = run_checked  # type: ignore[method-assign]
    with pytest.raises(BuildCancelled):
        engine._run_pipeline(
            SimpleNamespace(key="n60pro", profile="netcore_n60-pro"),
            SimpleNamespace(snapshot_id="snapshot"),
            openwrt,
            config,
            lines.append,
            cancel_event,
            jobs=4,
        )

    assert commands == [["make", "defconfig"], ["make", "download"], ["make", "-j4"]]
    assert any("跳过详细诊断编译" in line for line in lines)


def test_explicit_packages_keep_config_append_hardware_baseline(tmp_path: Path) -> None:
    """Web package replacement must not remove board-required append symbols."""

    repo_root = tmp_path / "repo"
    config_root = repo_root / "configs"
    config_root.mkdir(parents=True)
    (config_root / "n60pro.config").write_text(
        """CONFIG_TARGET_mediatek_filogic_DEVICE_n60pro=y
CONFIG_PACKAGE_main-plugin=y
CONFIG_MAIN_PLUGIN_MODE=n
CONFIG_PACKAGE_optional-plugin=y
#CONFIG_APPEND=board.config
""",
        encoding="utf-8",
    )
    openwrt_dir = tmp_path / "openwrt"
    (openwrt_dir / "defconfig").mkdir(parents=True)
    (openwrt_dir / "defconfig" / "board.config").write_text(
        """CONFIG_CONNINFRA_AUTO_UP=y
CONFIG_MTK_MT_WIFI=m
CONFIG_MTK_WARP_V2=y
CONFIG_PACKAGE_kmod-mediatek_hnat=y
CONFIG_PACKAGE_kmod-mt_wifi=y
CONFIG_PACKAGE_kmod-warp=y
CONFIG_PACKAGE_main-plugin=y
CONFIG_MAIN_PLUGIN_MODE=n
""",
        encoding="utf-8",
    )

    spec = DeviceSpec(
        key="n60pro",
        aliases=(),
        platform="mediatek",
        source_id="test-source",
        profile="n60pro",
        config="n60pro.config",
        packager="mediatek",
        target="mediatek/filogic",
    )
    source = SourceSpec(
        id="test-source",
        url="https://example.invalid/source.git",
        branch="main",
        snapshot="0123456789abcdef0123456789abcdef01234567",
        platform="mediatek",
    )
    devices = DeviceCatalog(
        path=config_root / "devices.toml",
        default_config="n60pro.config",
        sources={source.id: source},
        devices={spec.key: spec},
    )
    packages = [
        PackageMetadata(
            name="main-plugin",
            options=[
                KconfigOption(
                    symbol="CONFIG_MAIN_PLUGIN_MODE",
                    kind="bool",
                    package="main-plugin",
                ),
            ],
        ),
        PackageMetadata(name="optional-plugin"),
        PackageMetadata(name="kmod-mediatek_hnat"),
        PackageMetadata(name="kmod-mt_wifi"),
        PackageMetadata(name="kmod-warp"),
    ]
    catalog = Catalog(root=openwrt_dir, packages=packages, authoritative=True)
    engine = BuildEngine(
        repo_root=repo_root,
        workspace=tmp_path / "workspace",
        catalog=devices,
        use_docker=False,
    )
    build_dir = tmp_path / "build"
    build_dir.mkdir()

    output = engine._write_config(
        spec,
        source,
        openwrt_dir,
        build_dir,
        ("main-plugin",),
        {"CONFIG_MAIN_PLUGIN_MODE": True},
        catalog,
    )
    text = output.read_text(encoding="utf-8")

    assert "CONFIG_PACKAGE_kmod-mediatek_hnat=y" in text
    assert "CONFIG_PACKAGE_kmod-mt_wifi=y" in text
    assert "CONFIG_PACKAGE_kmod-warp=y" in text
    assert "CONFIG_CONNINFRA_AUTO_UP=y" in text
    assert "CONFIG_MTK_MT_WIFI=m" in text
    assert "CONFIG_MTK_WARP_V2=y" in text
    assert "CONFIG_PACKAGE_optional-plugin=y" not in text
    assert "CONFIG_PACKAGE_main-plugin=y" in text
    assert "CONFIG_MAIN_PLUGIN_MODE=y" in text
    assert text.count("CONFIG_PACKAGE_main-plugin=") == 1
    assert text.count("CONFIG_MAIN_PLUGIN_MODE=") == 1
    assert text.count("CONFIG_PACKAGE_kmod-mediatek_hnat=") == 1


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
