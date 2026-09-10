from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from owrt_builder.arm_packager import (
    ARM_PACKIT_TIMEOUT_SECONDS,
    ArmPackagerError,
    _run,
)


def test_packit_script_timeout_reports_loop_device_diagnostic(tmp_path: Path) -> None:
    lines: list[str] = []

    with pytest.raises(ArmPackagerError, match=r"packit script timed out after 0\.1 seconds"):
        _run(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=os.environ.copy(),
            callback=lines.append,
            cancel_event=None,
            timeout_seconds=0.1,
        )

    assert any("loop partition device" in line for line in lines)
    assert any("/dev bind mounted" in line for line in lines)


def test_packit_script_timeout_is_finite_and_below_worker_lease() -> None:
    assert 0 < ARM_PACKIT_TIMEOUT_SECONDS < 6 * 60 * 60
