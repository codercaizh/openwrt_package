from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from owrt_builder.system import SystemMonitor


def _proc_fixture(root: Path) -> None:
    root.mkdir()
    (root / "stat").write_text("cpu 100 0 50 850 0 0 0 0 0 0\n", encoding="utf-8")
    (root / "loadavg").write_text("1.20 0.80 0.40 1/20 10\n", encoding="utf-8")
    (root / "meminfo").write_text(
        "MemTotal:       1024 kB\nMemAvailable:    512 kB\n",
        encoding="utf-8",
    )
    (root / "uptime").write_text("120.00 0.00\n", encoding="utf-8")
    process = root / "42"
    process.mkdir()
    # pid (1), comm (2), state (3), ... utime (14), stime (15), ...
    fields = ["42", "worker name", "R"] + ["0"] * 10 + ["100", "20"] + ["0"] * 6 + ["100"]
    (process / "stat").write_text("42 (worker name) " + " ".join(fields[2:]) + "\n", encoding="utf-8")
    (process / "statm").write_text("20 2 0 0 0 0 0\n", encoding="utf-8")


def test_system_monitor_uses_proc_fixture_and_never_exposes_command_data(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _proc_fixture(proc)
    ticks = iter((0.0, 1.0, 2.0))

    monitor = SystemMonitor(
        proc,
        clock=lambda: next(ticks, 2.0),
        wall_clock=lambda: "2026-09-10T00:00:00Z",
        statvfs=lambda _path: SimpleNamespace(
            f_blocks=100,
            f_bfree=30,
            f_bavail=25,
            f_frsize=4096,
            f_bsize=4096,
        ),
    )
    first = monitor.status(tmp_path)
    (proc / "stat").write_text("cpu 140 0 70 890 0 0 0 0 0 0\n", encoding="utf-8")
    second = monitor.status(tmp_path)
    assert second["disk"]["total_bytes"] == 409600
    assert second["disk"]["used_bytes"] == 286720
    assert second["disk"]["available_bytes"] == 102400
    assert second["disk"]["usage_percent"] == 73.7
    assert second["memory"]["usage_percent"] == 50.0
    assert second["cpu"]["load_1m"] == 1.2
    assert first["proc_root"] == str(proc)

    processes = monitor.processes(limit=100, sort="memory")
    assert processes["limit"] == 100
    assert processes["items"][0]["pid"] == 42
    assert processes["items"][0]["name"] == "worker name"
    assert "cmdline" not in processes["items"][0]
    assert "env" not in processes["items"][0]
    assert "elapsed_seconds" in processes["items"][0]


def test_disk_usage_distinguishes_reserved_blocks_from_available_blocks(tmp_path: Path) -> None:
    monitor = SystemMonitor(
        tmp_path,
        statvfs=lambda _path: SimpleNamespace(
            f_blocks=100,
            f_bfree=30,
            f_bavail=25,
            f_frsize=4096,
            f_bsize=4096,
        ),
    )

    disk = monitor.status(tmp_path)["disk"]

    assert disk["total_bytes"] == 100 * 4096
    assert disk["used_bytes"] == (100 - 30) * 4096
    assert disk["available_bytes"] == 25 * 4096
    assert disk["usage_percent"] == round(70 / (70 + 25) * 100, 1)
