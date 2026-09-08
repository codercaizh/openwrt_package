from __future__ import annotations

import json
from argparse import Namespace

from owrt_builder import cli


def test_doctor_uses_host_probe_forwarded_by_python_fallback(monkeypatch, capsys) -> None:
    """Ubuntu 22.04 can run doctor in the worker image without Docker CLI."""

    engine = cli.BuildEngine(cli._repo_root(), cli._repo_root() / ".test-doctor-workspace")
    monkeypatch.setenv("OWRT_DOCKER_SERVER_AVAILABLE", "1")
    monkeypatch.setenv("OWRT_DOCKER_SERVER_VERSION", "28.4.0")
    monkeypatch.setenv("OWRT_DOCKER_IMAGE_CONTEXT", engine._context_fingerprint())

    def unexpected_docker_call(*_args, **_kwargs):
        raise AssertionError("doctor should use the forwarded host probe")

    monkeypatch.setattr(cli.subprocess, "run", unexpected_docker_call)

    assert cli._run_doctor(Namespace(json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["docker"] == {
        "available": True,
        "server_version": "28.4.0",
        "error": None,
    }
    assert report["builder_image"]["current"] is True
