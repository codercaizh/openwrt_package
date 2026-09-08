from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from owrt_builder.build import BuildRequest
from owrt_builder.storage import Storage
from owrt_builder.web import QueueWorker, Settings


class _Notifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def send_async(self, _job, status: str, error: str | None = None) -> None:
        self.events.append((status, error))


def _create_job(storage: Storage, tmp_path: Path, job_id: str, *, status: str = "running") -> dict:
    storage.create_job(
        {
            "id": job_id,
            "device": "netcore_n60-pro",
            "config": "n60pro",
            "packages": [],
            "options": {},
            "source_snapshot": {"snapshot_id": "snapshot"},
            "status": status,
            "output_dir": str(tmp_path / "artifacts" / job_id),
        }
    )
    job = storage.get_job(job_id)
    assert job is not None
    return job


def test_running_cancel_stops_named_container_without_local_event(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "cancel-direct")

    class Build:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

    build = Build()
    worker = QueueWorker(SimpleNamespace(build=build), storage, Settings(data_dir=tmp_path), _Notifier())

    assert worker.request_cancel("cancel-direct") == "cancel_requested"
    assert build.cancelled == ["cancel-direct"]
    assert storage.is_cancel_requested("cancel-direct") is True
    assert any("取消请求" in row["line"] for row in storage.get_logs("cancel-direct"))


def test_worker_external_stop_nonzero_finishes_canceled_with_result(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "cancel-worker", status="queued")
    notifier = _Notifier()
    worker: QueueWorker

    class Build:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

        def build(self, _request: BuildRequest, **_kwargs):
            # Simulate docker run being stopped externally after it started.
            assert worker.request_cancel("cancel-worker") == "cancel_requested"
            return {"status": "failed", "success": False, "error": "worker exited 137"}

    build = Build()
    worker = QueueWorker(
        SimpleNamespace(request_type=BuildRequest, build=build),
        storage,
        Settings(data_dir=tmp_path),
        notifier,
    )
    job = storage.claim_next_job()
    assert job is not None
    worker._run_job(job)

    saved = storage.get_job("cancel-worker")
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["finished_at"]
    assert saved["error"] == "管理员取消任务"
    assert saved["result"]["status"] == "canceled"
    assert saved["result"]["success"] is False
    assert build.cancelled == ["cancel-worker"]
    assert notifier.events[-1][0] == "canceled"


def test_worker_exception_result_is_not_success(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "failed-worker", status="queued")
    notifier = _Notifier()

    class Build:
        def build(self, _request: BuildRequest, **_kwargs):
            raise RuntimeError("docker run exited 137")

    worker = QueueWorker(
        SimpleNamespace(request_type=BuildRequest, build=Build()),
        storage,
        Settings(data_dir=tmp_path),
        notifier,
    )
    job = storage.claim_next_job()
    assert job is not None
    worker._run_job(job)

    saved = storage.get_job("failed-worker")
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["result"]["success"] is False
    assert saved["result"]["ok"] is False
    assert notifier.events[-1][0] == "failed"


def test_cancel_wins_over_late_success_result_atomically(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "late-success")
    assert storage.request_cancel("late-success") == "cancel_requested"

    final_status = storage.finish_job(
        "late-success",
        "succeeded",
        None,
        {"status": "success", "success": True, "ok": True},
    )

    saved = storage.get_job("late-success")
    assert final_status == "canceled"
    assert saved is not None
    assert saved["status"] == "canceled"
    assert saved["finished_at"]
    assert saved["result"]["status"] == "canceled"
    assert saved["result"]["success"] is False
    assert saved["result"]["ok"] is False


def test_reconcile_cancel_requested_stops_container_and_preserves_canceled(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "cancel-reconcile")
    assert storage.request_cancel("cancel-reconcile") == "cancel_requested"

    class Build:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def inspect(self, job_id: str) -> str:
            assert job_id == "cancel-reconcile"
            return "running"

        def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

    build = Build()
    worker = QueueWorker(SimpleNamespace(build=build), storage, Settings(data_dir=tmp_path), _Notifier())
    worker.reconcile()

    saved = storage.get_job("cancel-reconcile")
    assert saved is not None
    assert build.cancelled == ["cancel-reconcile"]
    assert saved["status"] == "canceled"
    assert saved["finished_at"]
    assert saved["error"] == "管理员取消任务"
    assert saved["result"]["status"] == "canceled"


def test_terminal_cancel_is_idempotent_and_queued_cancel_finishes_immediately(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    _create_job(storage, tmp_path, "cancel-terminal", status="succeeded")
    _create_job(storage, tmp_path, "cancel-queued", status="queued")

    class Build:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel(self, job_id: str) -> None:
            self.cancelled.append(job_id)

    build = Build()
    worker = QueueWorker(SimpleNamespace(build=build), storage, Settings(data_dir=tmp_path), _Notifier())

    assert worker.request_cancel("cancel-terminal") == "succeeded"
    assert worker.request_cancel("cancel-queued") == "canceled"
    assert build.cancelled == []
    queued = storage.get_job("cancel-queued")
    assert queued is not None
    assert queued["status"] == "canceled"
    assert queued["finished_at"]
    assert queued["result"]["status"] == "canceled"
