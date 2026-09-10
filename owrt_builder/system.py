"""Low-overhead, dependency-free host status and process probes.

The Web process normally runs in a container.  When ``OWRT_HOST_PROC`` points
at a read-only host ``/proc`` bind mount, the same code reports host-wide
metrics; tests and standalone runs fall back to the local ``/proc`` tree.
Only process metadata needed by the console is read.  In particular this
module never reads ``cmdline`` or the process environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping


_MEMINFO_RE = re.compile(r"^(MemTotal|MemAvailable|MemFree|Buffers|Cached):\s+(\d+)\s*(\w+)?", re.MULTILINE)
_PAGE_SIZE = int(os.sysconf("SC_PAGE_SIZE"))
_CLK_TCK = int(os.sysconf("SC_CLK_TCK"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _percent(used: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, used * 100.0 / total)), 1)


def _number(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


@dataclass
class ProcessSample:
    cpu_ticks: int
    observed_at: float


class SystemMonitor:
    """Collect host status with injectable filesystem and clocks.

    ``proc_root`` may point to a fixture directory in tests.  ``statvfs`` and
    ``clock`` are injectable to make the status endpoint deterministic without
    adding a runtime dependency such as psutil.
    """

    def __init__(
        self,
        proc_root: str | os.PathLike[str] | None = None,
        *,
        statvfs: Callable[[str | os.PathLike[str]], os.statvfs_result] = os.statvfs,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] = _utc_now,
    ) -> None:
        configured = proc_root or os.getenv("OWRT_HOST_PROC") or "/proc"
        self.proc_root = Path(configured)
        self._statvfs = statvfs
        self._clock = clock
        self._wall_clock = wall_clock
        self._cpu_previous: tuple[int, int, float] | None = None
        self._process_previous: dict[tuple[int, int], ProcessSample] = {}

    def _proc(self) -> Path:
        """Use the configured host mount, falling back for local/dev runs."""

        try:
            if self.proc_root.is_dir():
                return self.proc_root
        except OSError:
            pass
        return Path("/proc")

    def _cpu_times(self) -> tuple[int, int] | None:
        try:
            line = next(line for line in _read(self._proc() / "stat").splitlines() if line.startswith("cpu "))
            values = [int(value) for value in line.split()[1:]]
        except (OSError, StopIteration, ValueError):
            return None
        if not values:
            return None
        # Linux cpu fields are user,nice,system,idle,iowait,irq,softirq,...
        total = sum(values)
        idle = values[3] if len(values) > 3 else 0
        if len(values) > 4:
            idle += values[4]
        return total, idle

    def _cpu(self) -> dict[str, Any]:
        logical = max(1, int(os.cpu_count() or 1))
        times = self._cpu_times()
        now = self._clock()
        usage: float | None = None
        if times is not None:
            total, idle = times
            previous = self._cpu_previous
            self._cpu_previous = (total, idle, now)
            if previous is not None:
                delta_total = total - previous[0]
                delta_idle = idle - previous[1]
                if delta_total > 0:
                    usage = round(max(0.0, min(100.0, (delta_total - delta_idle) * 100.0 / delta_total)), 1)
        loads: list[float] = []
        try:
            raw_load = _read(self._proc() / "loadavg").split()
            loads = [float(value) for value in raw_load[:3]]
        except (OSError, ValueError):
            try:
                loads = [float(value) for value in os.getloadavg()[:3]]
            except (OSError, ValueError):
                loads = []
        loads.extend([0.0] * (3 - len(loads)))
        load_values = [round(max(0.0, loads[index]), 2) for index in range(3)]
        return {
            "logical_cpus": logical,
            "cores": logical,
            "usage_percent": usage,
            "utilization_percent": usage,
            "load_1m": load_values[0],
            "load_5m": load_values[1],
            "load_15m": load_values[2],
            "load": load_values,
        }

    def _memory(self) -> dict[str, Any]:
        values: dict[str, int] = {}
        try:
            for match in _MEMINFO_RE.finditer(_read(self._proc() / "meminfo")):
                unit = (match.group(3) or "").lower()
                multiplier = 1024 if unit == "kb" else 1024 * 1024 if unit == "mb" else 1
                values[match.group(1)] = int(match.group(2)) * multiplier
        except (OSError, ValueError):
            values = {}
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable")
        if available is None:
            available = values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0)
        available = max(0, min(total, available))
        used = max(0, total - available)
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "free_bytes": available,
            "usage_percent": _percent(used, total),
        }

    def _disk(self, data_path: str | os.PathLike[str]) -> dict[str, Any]:
        path = str(data_path)
        try:
            stats = self._statvfs(path)
            size = _number(getattr(stats, "f_frsize", 0) or getattr(stats, "f_bsize", 0), 1)
            blocks = _number(getattr(stats, "f_blocks", 0))
            # f_bfree is the number of blocks not allocated at all, whereas
            # f_bavail excludes blocks reserved for privileged users.  Keep
            # both values distinct: usage is based on user-visible capacity
            # (used + available), while total reports the complete volume.
            raw_bfree = getattr(stats, "f_bfree", None)
            bfree = _number(raw_bfree if raw_bfree is not None else blocks - _number(getattr(stats, "f_bavail", 0)))
            bavail = _number(getattr(stats, "f_bavail", 0))
            total = blocks * size
            used = max(0, blocks - bfree) * size
            available = bavail * size
        except (OSError, AttributeError, TypeError, ValueError):
            total = used = available = bfree = 0
        return {
            "path": path,
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "free_bytes": bfree * size if total else 0,
            "usage_percent": _percent(used, used + available),
        }

    def status(self, data_path: str | os.PathLike[str] = ".") -> dict[str, Any]:
        """Return one lightweight status sample for the configured data volume."""

        return {
            "ok": True,
            "collected_at": self._wall_clock(),
            "proc_root": str(self._proc()),
            "disk": self._disk(data_path),
            "memory": self._memory(),
            "cpu": self._cpu(),
        }

    @staticmethod
    def _parse_stat(raw: str) -> tuple[str, str, int, int] | None:
        # comm is wrapped in parentheses and may itself contain spaces.  The
        # final ')' is the delimiter for the remaining fields.
        close = raw.rfind(")")
        if close < 0:
            return None
        head = raw[:close]
        open_paren = head.find("(")
        if open_paren < 0:
            return None
        name = head[open_paren + 1 : close]
        fields = raw[close + 2 :].split()
        # After comm, fields[0] is state; utime/stime are fields 12/13 and
        # starttime is field 20 in this zero-based remainder.
        if len(fields) <= 19:
            return None
        try:
            return name, fields[0][:1] or "?", int(fields[11]) + int(fields[12]), int(fields[19])
        except (TypeError, ValueError, IndexError):
            return None

    def _rss(self, pid_path: Path) -> int:
        try:
            fields = _read(pid_path / "statm").split()
            if len(fields) > 1:
                return _number(fields[1]) * _PAGE_SIZE
        except (OSError, ValueError):
            pass
        try:
            for line in _read(pid_path / "status").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    multiplier = 1024 if len(parts) > 2 and parts[2].lower() == "kb" else 1
                    return _number(parts[1]) * multiplier
        except (OSError, ValueError, IndexError):
            pass
        return 0

    def _uptime(self) -> float:
        try:
            return max(0.0, float(_read(self._proc() / "uptime").split()[0]))
        except (OSError, ValueError, IndexError):
            return 0.0

    def processes(self, *, limit: int = 50, sort: str = "cpu", order: str = "desc") -> dict[str, Any]:
        """Return bounded process metadata without command lines or env vars."""

        # The Web API validates this range too.  Keep the guard here because
        # callers may use SystemMonitor directly (and because an untrusted
        # query must never turn into an unbounded ``/proc`` response).
        limit = max(1, min(100, int(limit)))
        sort = sort if sort in {"cpu", "memory"} else "cpu"
        order = order if order in {"asc", "desc"} else "desc"
        root = self._proc()
        now = self._clock()
        uptime = self._uptime()
        memory = self._memory()
        total_memory = int(memory.get("total_bytes", 0) or 0)
        result: list[dict[str, Any]] = []
        current_keys: set[tuple[int, int]] = set()
        try:
            entries = list(root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit() or not entry.is_dir():
                continue
            try:
                pid = int(entry.name)
                parsed = self._parse_stat(_read(entry / "stat"))
                if parsed is None:
                    continue
                name, state, cpu_ticks, start_ticks = parsed
                key = (pid, start_ticks)
                current_keys.add(key)
                previous = self._process_previous.get(key)
                cpu_percent = 0.0
                if previous is not None and now > previous.observed_at:
                    cpu_percent = max(0.0, (cpu_ticks - previous.cpu_ticks) / _CLK_TCK / (now - previous.observed_at) * 100.0)
                self._process_previous[key] = ProcessSample(cpu_ticks, now)
                rss = self._rss(entry)
                elapsed = round(max(0.0, uptime - start_ticks / _CLK_TCK), 1)
                result.append({
                    "pid": pid,
                    "name": name[:256],
                    "state": state,
                    "cpu_percent": round(cpu_percent, 1),
                    "memory_bytes": rss,
                    "memory_percent": _percent(rss, total_memory),
                    # ``elapsed_seconds`` is the canonical API field;
                    # ``runtime_seconds`` remains for older dashboards.
                    "elapsed_seconds": elapsed,
                    "elapsed": elapsed,
                    "runtime_seconds": elapsed,
                })
            except (OSError, ValueError, OverflowError):
                # Processes can disappear between directory enumeration and
                # stat reads; they are intentionally omitted from this sample.
                continue
        # Drop exited processes so a long-running Web process cannot retain
        # unbounded PID/start-time samples.
        self._process_previous = {key: value for key, value in self._process_previous.items() if key in current_keys}
        reverse = order == "desc"
        field = "cpu_percent" if sort == "cpu" else "memory_bytes"
        result.sort(key=lambda item: (float(item[field]), int(item["pid"])), reverse=reverse)
        return {
            "ok": True,
            "collected_at": self._wall_clock(),
            "items": result[:limit],
            "total": len(result),
            "limit": limit,
            "sort": sort,
            "order": order,
        }


__all__ = ["SystemMonitor"]
