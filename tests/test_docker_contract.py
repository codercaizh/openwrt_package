from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _stage(source: str, marker: str, next_marker: str) -> str:
    start = source.index(marker)
    end = source.index(next_marker, start)
    return source[start:end]


def test_web_stage_has_docker_cli_and_worker_stage_does_not() -> None:
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    web = _stage(dockerfile, "FROM owrt-base AS web", "FROM owrt-base AS worker")
    worker = dockerfile[dockerfile.index("FROM owrt-base AS worker") :]

    assert "apt-get install -y --no-install-recommends docker.io" in web
    assert "docker.io" not in worker
    assert "TZ=Asia/Shanghai" in dockerfile
    assert "ENV TZ=Asia/Shanghai" in worker


def test_web_compose_mounts_host_docker_socket() -> None:
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    web = compose[compose.index("  web:\n") :]

    assert "/var/run/docker.sock:/var/run/docker.sock" in web
    assert "caddy" not in compose
    assert "${OWRT_WEB_PORT:-8000}:8000" in compose
    assert "TZ: Asia/Shanghai" in web
    assert "OWRT_STATE_HOST_PATH" in web
    assert "OWRT_WORKSPACE_HOST_PATH" in web
    assert "OWRT_ARTIFACT_HOST_PATH" in web
    assert "OWRT_DATA_HOST_PATH" in web


def test_single_container_launcher_preserves_host_paths_for_worker_mounts() -> None:
    launcher = ROOT / "run-web"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111
    result = __import__("subprocess").run(
        [str(launcher), "18000"],
        cwd=ROOT,
        env={
            "PATH": __import__("os").environ["PATH"],
            "OWRT_START_WEB_DRY_RUN": "1",
            "OWRT_REPO_HOST_PATH": str(ROOT),
            "OWRT_STATE_HOST_PATH": str(ROOT / ".owrt-web" / "state"),
            "OWRT_WORKSPACE_HOST_PATH": str(ROOT / ".owrt-web" / "workspace"),
            "OWRT_ARTIFACT_HOST_PATH": str(ROOT / ".owrt-web" / "state" / "artifacts"),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert "repo_root=" + str(ROOT) in result.stdout
    assert "data_root=" + str(ROOT / ".owrt-web") in result.stdout
    assert "state_root=" + str(ROOT / ".owrt-web" / "state") in result.stdout
    assert "workspace_root=" + str(ROOT / ".owrt-web" / "workspace") in result.stdout
    assert "artifact_root=" + str(ROOT / ".owrt-web" / "state" / "artifacts") in result.stdout
    assert "port=18000" in result.stdout
    assert "docker_socket=/var/run/docker.sock" in result.stdout

    source = launcher.read_text(encoding="utf-8")
    assert '"$repo_root:$repo_root:ro"' in source
    assert '"$state_root:$state_root:rw"' in source
    assert '"$workspace_root:$workspace_root:rw"' in source
    assert '"$artifact_root:$artifact_root:rw"' in source
    assert '--volume /var/run/docker.sock:/var/run/docker.sock' in source
    assert '--env TZ=Asia/Shanghai' in source


def test_single_container_launcher_passes_same_host_paths_to_docker(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker-args.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$DOCKER_ARGS_LOG\"\n"
        "case \"$1\" in build|ps) exit 0;; run) echo fake-container; exit 0;; esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "DOCKER_ARGS_LOG": str(log),
            "OWRT_REPO_HOST_PATH": str(ROOT),
            "OWRT_DATA_HOST_PATH": str(tmp_path / "persistent"),
            "OWRT_STATE_HOST_PATH": "",
            "OWRT_WORKSPACE_HOST_PATH": "",
            "OWRT_ARTIFACT_HOST_PATH": "",
            "OWRT_WEB_CONTAINER_NAME": "contract-web",
        }
    )
    result = subprocess.run([str(ROOT / "run-web"), "18001"], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"build --target web --file {ROOT}/docker/Dockerfile" in calls
    assert f"--volume {ROOT}:{ROOT}:ro" in calls
    assert f"--volume {tmp_path / 'persistent' / 'state'}:{tmp_path / 'persistent' / 'state'}:rw" in calls
    assert f"--volume {tmp_path / 'persistent' / 'workspace'}:{tmp_path / 'persistent' / 'workspace'}:rw" in calls
    assert f"--volume {tmp_path / 'persistent' / 'state' / 'artifacts'}:{tmp_path / 'persistent' / 'state' / 'artifacts'}:rw" in calls
    assert "--volume /var/run/docker.sock:/var/run/docker.sock" in calls
    assert "--publish 18001:8000" in calls


def test_dependency_free_launcher_mounts_host_dev_only_for_arm_builds(tmp_path: Path) -> None:
    """The fallback Docker path exposes loop partition nodes only to ARM."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker-args.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$DOCKER_ARGS_LOG\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "DOCKER_ARGS_LOG": str(log),
            "OWRT_BUILDER_IMAGE": "contract-builder:local",
            "OWRT_CLI_WORKSPACE_HOST_PATH": str(tmp_path / "cli-workspace"),
        }
    )

    arm = subprocess.run(
        [str(ROOT / "owrt"), "build", "s905d"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert arm.returncode == 0
    arm_runs = [line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")]
    assert arm_runs
    assert "--privileged" in arm_runs[0]
    assert "--volume /dev:/dev" in arm_runs[0]

    log.write_text("", encoding="utf-8")
    non_arm = subprocess.run(
        [str(ROOT / "owrt"), "build", "n60pro"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert non_arm.returncode == 0
    non_arm_runs = [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    ]
    assert non_arm_runs
    assert all("--privileged" not in line for line in non_arm_runs)
    assert all("--volume /dev:/dev" not in line for line in non_arm_runs)


def test_dependency_free_launcher_maps_cli_workspace_for_host_python(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    capture = tmp_path / "workspace-path"
    fake_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        "printf '%s' \"$OWRT_WORKSPACE\" > \"$OWRT_CAPTURE\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "OWRT_CLI_WORKSPACE_HOST_PATH": str(tmp_path / "cli-workspace"),
            "OWRT_CAPTURE": str(capture),
        }
    )
    result = subprocess.run([str(ROOT / "owrt"), "devices"], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8") == str(tmp_path / "cli-workspace")
