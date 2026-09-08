from __future__ import annotations

from pathlib import Path


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


def test_web_compose_mounts_host_docker_socket() -> None:
    compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
    web = _stage(compose, "  web:\n", "\n  caddy:\n")

    assert "/var/run/docker.sock:/var/run/docker.sock" in web
