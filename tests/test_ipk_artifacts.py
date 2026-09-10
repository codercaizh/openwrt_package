from __future__ import annotations

import tarfile
from pathlib import Path

from owrt_builder.build import BuildEngine, IPK_ARCHIVE_NAME
from owrt_builder.storage import Storage
from owrt_builder.web import QueueWorker


def test_ipk_archive_contains_only_packages_changed_by_this_build(tmp_path: Path) -> None:
    openwrt = tmp_path / "openwrt"
    package_dir = openwrt / "bin" / "packages" / "aarch64_cortex-a53" / "base"
    package_dir.mkdir(parents=True)
    stale = package_dir / "stale.ipk"
    changed = package_dir / "changed.ipk"
    new = package_dir / "new.ipk"
    stale.write_bytes(b"stale package")
    changed.write_bytes(b"before-build")

    engine = BuildEngine.__new__(BuildEngine)
    before = engine._snapshot_ipk_outputs(openwrt)

    # Keep the changed package the same size so the content digest, rather
    # than only mtime/size, proves that replacement is detected.
    changed.write_bytes(b"after-build!")
    new.write_bytes(b"new package")
    archive = engine._package_ipk_archive(
        openwrt,
        tmp_path / "artifacts",
        before,
        lambda _line: None,
        None,
    )

    assert archive == tmp_path / "artifacts" / IPK_ARCHIVE_NAME
    with tarfile.open(archive, mode="r:gz") as output:
        assert sorted(output.getnames()) == [
            "packages/aarch64_cortex-a53/base/changed.ipk",
            "packages/aarch64_cortex-a53/base/new.ipk",
        ]
        assert output.extractfile("packages/aarch64_cortex-a53/base/changed.ipk").read() == b"after-build!"
    assert "stale.ipk" not in archive.read_bytes().decode("latin1", errors="ignore")


def test_ipk_archive_is_not_created_for_an_unchanged_cache(tmp_path: Path) -> None:
    openwrt = tmp_path / "openwrt"
    package_dir = openwrt / "bin" / "packages" / "x" / "base"
    package_dir.mkdir(parents=True)
    (package_dir / "cached.ipk").write_bytes(b"cached package")

    engine = BuildEngine.__new__(BuildEngine)
    before = engine._snapshot_ipk_outputs(openwrt)
    archive = engine._package_ipk_archive(
        openwrt,
        tmp_path / "artifacts",
        before,
        lambda _line: None,
        None,
    )

    assert archive is None
    assert not (tmp_path / "artifacts" / IPK_ARCHIVE_NAME).exists()


def test_web_registers_ipk_archive_as_a_downloadable_artifact(tmp_path: Path) -> None:
    output = tmp_path / "artifacts" / "netcore_n60-pro" / "job-ipk"
    output.mkdir(parents=True)
    workspace_archive = tmp_path / IPK_ARCHIVE_NAME
    workspace_archive.write_bytes(b"compressed ipks")
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    job = {
        "id": "job-ipk",
        "device": "netcore_n60-pro",
        "config": "n60pro",
        "packages": [],
        "options": {},
        "source_snapshot": {},
        "status": "running",
        "output_dir": str(output),
    }
    storage.create_job(job)

    worker = QueueWorker.__new__(QueueWorker)
    worker.storage = storage
    worker._register_artifacts(job, {"artifacts": [str(workspace_archive)]})

    rows = storage.list_artifacts("job-ipk")
    assert [row["name"] for row in rows] == [IPK_ARCHIVE_NAME]
    assert (output / IPK_ARCHIVE_NAME).read_bytes() == b"compressed ipks"


def test_github_actions_uploads_ipk_archive_separately() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "name: Upload IPK package archive" in text
    assert "path: .owrt/artifacts/**/ipk-packages.tar.gz" in text
    assert "!.owrt/artifacts/**/ipk-packages.tar.gz" in text
