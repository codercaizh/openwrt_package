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


def test_single_container_launcher_preserves_host_paths_for_worker_mounts() -> None:
    launcher = ROOT / "run-web"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111
    result = __import__("subprocess").run(
        [str(launcher), "18000"],
        cwd=ROOT,
        env={"PATH": __import__("os").environ["PATH"], "OWRT_START_WEB_DRY_RUN": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert "repo_root=" + str(ROOT) in result.stdout
    assert "data_root=" + str(ROOT / ".owrt-web") in result.stdout
    assert "port=18000" in result.stdout
    assert "docker_socket=/var/run/docker.sock" in result.stdout

    source = launcher.read_text(encoding="utf-8")
    assert '"$repo_root:$repo_root:ro"' in source
    assert '"$data_root:$data_root:rw"' in source
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
            "OWRT_DATA_HOST_PATH": str(tmp_path / "persistent"),
            "OWRT_WEB_CONTAINER_NAME": "contract-web",
        }
    )
    result = subprocess.run([str(ROOT / "run-web"), "18001"], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"build --target web --file {ROOT}/docker/Dockerfile" in calls
    assert f"--volume {ROOT}:{ROOT}:ro" in calls
    assert f"--volume {tmp_path / 'persistent'}:{tmp_path / 'persistent'}:rw" in calls
    assert "--volume /var/run/docker.sock:/var/run/docker.sock" in calls
    assert "--publish 18001:8000" in calls
