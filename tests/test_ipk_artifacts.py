from __future__ import annotations

import tarfile
from pathlib import Path

from owrt_builder.build import BuildEngine, IPK_ARCHIVE_NAME, PACKAGE_ARCHIVE_NAME
from owrt_builder.storage import Storage
from owrt_builder.web import QueueWorker


def _archive(
    tmp_path: Path,
    files: dict[str, bytes],
    changed: dict[str, bytes] | None = None,
) -> tuple[Path | None, list[str]]:
    """Create a package tree, snapshot it, then apply build output changes."""

    openwrt = tmp_path / "openwrt"
    package_dir = openwrt / "bin" / "packages" / "aarch64_cortex-a53" / "base"
    package_dir.mkdir(parents=True)
    for name, content in files.items():
        (package_dir / name).write_bytes(content)

    engine = BuildEngine.__new__(BuildEngine)
    before = engine._snapshot_package_outputs(openwrt)
    for name, content in (changed or {}).items():
        (package_dir / name).write_bytes(content)

    messages: list[str] = []
    archive = engine._package_archive(
        openwrt,
        tmp_path / "artifacts",
        before,
        messages.append,
        None,
    )
    if archive is None:
        return None, messages
    with tarfile.open(archive, mode="r:gz") as output:
        return archive, output.getnames()


def test_apk_archive_contains_only_apk_outputs_changed_by_this_build(tmp_path: Path) -> None:
    archive, members = _archive(
        tmp_path,
        {"stale.apk": b"stale", "changed.apk": b"before"},
        {"changed.apk": b"after!", "new.apk": b"new"},
    )

    assert archive == tmp_path / "artifacts" / PACKAGE_ARCHIVE_NAME
    assert sorted(members) == [
        "packages/aarch64_cortex-a53/base/changed.apk",
        "packages/aarch64_cortex-a53/base/new.apk",
    ]
    with tarfile.open(archive, mode="r:gz") as output:
        assert output.extractfile("packages/aarch64_cortex-a53/base/changed.apk").read() == b"after!"


def test_ipk_archive_contains_only_ipk_outputs_changed_by_this_build(tmp_path: Path) -> None:
    archive, members = _archive(
        tmp_path,
        {"stale.ipk": b"stale", "changed.ipk": b"before"},
        {"changed.ipk": b"after!", "new.ipk": b"new"},
    )

    assert archive == tmp_path / "artifacts" / PACKAGE_ARCHIVE_NAME
    assert sorted(members) == [
        "packages/aarch64_cortex-a53/base/changed.ipk",
        "packages/aarch64_cortex-a53/base/new.ipk",
    ]
    with tarfile.open(archive, mode="r:gz") as output:
        assert output.extractfile("packages/aarch64_cortex-a53/base/changed.ipk").read() == b"after!"


def test_mixed_package_archive_contains_apk_and_ipk_outputs(tmp_path: Path) -> None:
    archive, members = _archive(
        tmp_path,
        {"cached.apk": b"cached apk", "cached.ipk": b"cached ipk"},
        {"new.apk": b"new apk", "new.ipk": b"new ipk"},
    )

    assert archive == tmp_path / "artifacts" / PACKAGE_ARCHIVE_NAME
    assert sorted(members) == [
        "packages/aarch64_cortex-a53/base/new.apk",
        "packages/aarch64_cortex-a53/base/new.ipk",
    ]


def test_package_archive_is_not_created_for_an_unchanged_cache(tmp_path: Path) -> None:
    archive, messages = _archive(
        tmp_path,
        {"cached.apk": b"cached apk", "cached.ipk": b"cached ipk"},
    )

    assert archive is None
    assert not (tmp_path / "artifacts" / PACKAGE_ARCHIVE_NAME).exists()
    assert messages == ["本次编译未生成新的软件包，不创建软件包归档"]


def test_web_registers_package_archives_as_downloadable_artifacts(tmp_path: Path) -> None:
    output = tmp_path / "artifacts" / "netcore_n60-pro" / "job-package"
    output.mkdir(parents=True)
    workspace_archive = tmp_path / PACKAGE_ARCHIVE_NAME
    workspace_archive.write_bytes(b"compressed package archive")
    legacy_archive = tmp_path / IPK_ARCHIVE_NAME
    legacy_archive.write_bytes(b"compressed legacy archive")
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    job = {
        "id": "job-package",
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
    worker._register_artifacts(job, {"artifacts": [str(workspace_archive), str(legacy_archive)]})

    rows = storage.list_artifacts("job-package")
    assert [row["name"] for row in rows] == [IPK_ARCHIVE_NAME, PACKAGE_ARCHIVE_NAME]
    assert (output / PACKAGE_ARCHIVE_NAME).read_bytes() == b"compressed package archive"
    assert (output / IPK_ARCHIVE_NAME).read_bytes() == b"compressed legacy archive"


def test_github_actions_uploads_apk_ipk_archive_separately() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
    text = workflow.read_text(encoding="utf-8")
    firmware_step = text[text.index("- name: Upload firmware") : text.index("- name: Upload APK/IPK package archive")]
    package_step = text[text.index("- name: Upload APK/IPK package archive") : text.index("- name: Upload build evidence")]

    assert "name: Upload APK/IPK package archive" in text
    assert "name: openwrt-packages-${{ inputs.device || github.event.inputs.device }}-${{ github.run_id }}" in text
    assert "!.owrt/artifacts/**/packages.tar.gz" in firmware_step
    assert "!.owrt/artifacts/**/ipk-packages.tar.gz" in firmware_step
    assert "path: |\n            .owrt/artifacts/**/packages.tar.gz" in package_step
    assert ".owrt/artifacts/**/ipk-packages.tar.gz" in package_step
