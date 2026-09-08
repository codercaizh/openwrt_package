from __future__ import annotations

from pathlib import Path

from owrt_builder.storage import Storage


def _storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    storage.create_job(
        {
            "id": "log-job",
            "device": "netcore_n60-pro",
            "config": "n60pro",
            "packages": [],
            "options": {},
            "source_snapshot": {},
            "output_dir": str(tmp_path / "artifacts" / "log-job"),
        }
    )
    return storage


def test_large_file_backed_log_reconnects_without_duplicate_sequences(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    for index in range(100_000):
        storage.append_log("log-job", f"line-{index}")

    tail = storage.get_logs("log-job", 99_997, 10)
    assert [row["seq"] for row in tail] == [99_998, 99_999, 100_000]

    # A fresh Storage object simulates a Web process restart.  It must recover
    # the JSONL tail from the periodic checkpoint without reusing a sequence.
    restarted = Storage(tmp_path / "state.sqlite3", tmp_path / "logs", tmp_path / "artifacts")
    restarted.append_log("log-job", "after-restart")
    tail = restarted.get_logs("log-job", 100_000, 2)
    assert [row["seq"] for row in tail] == [100_001]
    assert tail[0]["line"] == "after-restart"
