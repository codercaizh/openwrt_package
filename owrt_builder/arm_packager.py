"""Internal ARM image packager.

The OpenWrt ``armsr/armv8`` target produces a generic rootfs archive.  The
two supported ARM boards need the small, board-specific scripts from
``unifreq/openwrt_packit`` to turn that archive into a bootable image.  This
module is deliberately private to :class:`BuildEngine`: it has no compile,
download-only, menuconfig, x86, or arbitrary-device modes.

Every external operation is checked.  A missing packit script, an incomplete
kernel archive, or a failed image command therefore fails the build instead of
leaving a misleading success artifact behind.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


LogCallback = Callable[[str], None]


class ArmPackagerError(RuntimeError):
    """The ARM image packaging step could not complete."""


_PACKIT_SCRIPTS: Mapping[str, str] = {
    "s905d": "mk_s905d_n1.sh",
    "vplus": "mk_h6_vplus.sh",
}


@dataclass(frozen=True)
class ArmPackageResult:
    """Files created by the private ARM packager."""

    artifacts: tuple[Path, ...]


def _emit(callback: LogCallback, line: str) -> None:
    callback(line)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    callback: LogCallback,
    cancel_event: Any,
) -> None:
    """Run one command while forwarding output and honoring cancellation."""

    _emit(callback, "$ " + " ".join(str(item) for item in command))
    process = subprocess.Popen(
        [str(item) for item in command],
        cwd=str(cwd),
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        start_new_session=(os.name == "posix"),
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if cancel_event is not None and cancel_event.is_set():
                if os.name == "posix":
                    os.killpg(process.pid, 15)
                else:
                    process.terminate()
                raise ArmPackagerError("ARM packaging cancelled")
            _emit(callback, line.rstrip("\n"))
        returncode = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if returncode != 0:
        rendered = " ".join(str(item) for item in command)
        raise ArmPackagerError(f"command exited {returncode}: {rendered}")


def _remove_children(path: Path) -> None:
    """Remove only the contents of a known private workspace directory."""

    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def _kernel_asset(
    *,
    callback: LogCallback,
    cancel_event: Any,
) -> tuple[str, str]:
    """Return the latest stable kernel archive name and URL."""

    command = [
        "curl",
        "-fsSL",
        "--retry",
        "3",
        "--connect-timeout",
        "20",
        "--max-time",
        "120",
        "-H",
        "Accept: application/vnd.github+json",
        "https://api.github.com/repos/codercaizh/openwrt_package/releases/tags/kernel_stable",
    ]
    _emit(callback, "$ " + " ".join(command[:-1]) + " <GitHub release metadata>")
    if cancel_event is not None and cancel_event.is_set():
        raise ArmPackagerError("ARM packaging cancelled")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise ArmPackagerError(f"unable to query stable ARM kernel release: {detail}") from exc
    assets = payload.get("assets") if isinstance(payload, Mapping) else None
    # The stable release is a single 7z bundle containing the matching
    # ``boot-*``, ``modules-*`` and board DTB archives.  Select the bundle,
    # then derive the kernel version from the extracted boot archive.  Older
    # releases exposed the payload under a different filename, so accepting
    # any 7z/zip asset keeps the reviewed source independent of that naming
    # detail while still rejecting unrelated release metadata.
    candidates = [
        item
        for item in (assets or ())
        if isinstance(item, Mapping)
        and str(item.get("name", "")).lower().endswith((".7z", ".7zip", ".zip"))
        and str(item.get("browser_download_url", ""))
    ]
    if not candidates:
        raise ArmPackagerError("stable ARM kernel release has no kernel bundle")

    def version_key(item: Mapping[str, Any]) -> tuple[tuple[int, ...], str]:
        name = str(item.get("name", ""))
        return tuple(int(part) for part in re.findall(r"\d+", name)), name

    selected = max(candidates, key=version_key)
    name = str(selected["name"]).strip()
    # Release metadata is remote input.  Keep the downloaded archive inside
    # the private kernel cache even if a malformed or compromised API response
    # contains a path separator or control character.
    if (
        not name
        or Path(name).name != name
        or name in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}", name)
    ):
        raise ArmPackagerError(f"stable ARM kernel release has an invalid asset name: {name!r}")
    url = str(selected["browser_download_url"]).strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or any(ch.isspace() for ch in url):
        raise ArmPackagerError("stable ARM kernel release has an invalid asset URL")
    return name, url


def _find_kernel_version(kernel_dir: Path) -> str:
    archives = sorted(
        path
        for path in kernel_dir.glob("boot-*")
        if path.is_file() and path.name.endswith(".tar.gz")
    )
    if not archives:
        raise ArmPackagerError(f"kernel archive extraction produced no boot-*.tar.gz in {kernel_dir}")
    name = archives[-1].name.removesuffix(".tar.gz")
    version = name.removeprefix("boot-")
    if not version:
        raise ArmPackagerError(f"invalid ARM kernel archive name: {archives[-1].name}")
    return version


def _clear_kernel_payload(kernel_dir: Path) -> None:
    """Remove extracted kernel payloads while retaining downloaded bundles."""

    for pattern in ("boot-*", "modules-*", "dtb-*"):
        for path in kernel_dir.glob(pattern):
            # A future release could name its bundle boot-*.7z.  Preserve
            # downloaded archives and clear only extracted files/directories.
            if path.is_file() and path.name.lower().endswith((".7z", ".7zip", ".zip", ".tar.gz")):
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)


def package_arm(
    *,
    device: str,
    rootfs_path: str | Path,
    workspace: str | Path,
    callback: LogCallback,
    cancel_event: Any = None,
) -> ArmPackageResult:
    """Create a bootable image for one of the supported ARM boards."""

    try:
        script_name = _PACKIT_SCRIPTS[device]
    except KeyError as exc:
        raise ArmPackagerError(
            f"unsupported ARM packaging device {device!r}; expected s905d or vplus"
        ) from exc

    rootfs = Path(rootfs_path).expanduser().resolve()
    if not rootfs.is_file() or rootfs.stat().st_size <= 0:
        raise ArmPackagerError(f"ARM rootfs archive is missing or empty: {rootfs}")

    arm_root = Path(workspace).expanduser().resolve() / "arm-package"
    packit_dir = arm_root / "packit"
    kernel_dir = arm_root / "kernel"
    output_dir = arm_root / "artifact" / device
    arm_root.mkdir(parents=True, exist_ok=True)
    kernel_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_children(output_dir)

    if not (packit_dir / ".git").is_dir():
        if packit_dir.exists():
            shutil.rmtree(packit_dir)
        _run(
            ["git", "clone", "--depth", "1", "https://github.com/unifreq/openwrt_packit", str(packit_dir)],
            cwd=arm_root,
            env=os.environ.copy(),
            callback=callback,
            cancel_event=cancel_event,
        )

    script = packit_dir / script_name
    if not script.is_file():
        raise ArmPackagerError(f"packit script missing for {device}: {script_name}")

    asset_name, asset_url = _kernel_asset(callback=callback, cancel_event=cancel_event)
    archive = kernel_dir / asset_name
    if not archive.is_file() or archive.stat().st_size <= 0:
        _run(
            [
                "curl",
                "-fL",
                "--retry",
                "3",
                "--connect-timeout",
                "20",
                "--max-time",
                "1800",
                "-o",
                str(archive),
                asset_url,
            ],
            cwd=kernel_dir,
            env=os.environ.copy(),
            callback=callback,
            cancel_event=cancel_event,
        )
    if archive.stat().st_size <= 0:
        raise ArmPackagerError(f"downloaded ARM kernel archive is empty: {archive}")
    _clear_kernel_payload(kernel_dir)
    _run(
        ["7z", "x", str(archive), "-y", f"-o{kernel_dir}"],
        cwd=kernel_dir,
        env=os.environ.copy(),
        callback=callback,
        cancel_event=cancel_event,
    )
    kernel_version = _find_kernel_version(kernel_dir)

    _remove_children(packit_dir / "output")
    _remove_children(packit_dir / "tmp")
    for previous in packit_dir.glob("*rootfs.tar.gz"):
        if previous.is_file() or previous.is_symlink():
            previous.unlink()
    rootfs_copy = packit_dir / rootfs.name
    shutil.copyfile(rootfs, rootfs_copy)

    env = os.environ.copy()
    env.update(
        {
            "OP_ROOT_TGZ": rootfs_copy.name,
            "KERNEL_VERSION": kernel_version,
            "KERNEL_PKG_HOME": str(kernel_dir),
            "OPENWRT_VER": os.environ.get("OWRT_OPENWRT_VERSION", "R25.12"),
            "SW_FLOWOFFLOAD": "0",
            "SFE_FLOW": "0",
            "ENABLE_WIFI_K504": "0",
            "ENABLE_WIFI_K510": "0",
            "PACKIT_DIR": str(packit_dir),
        }
    )
    _emit(callback, f"使用 ARM packit 脚本: {script_name}")
    _run(
        ["bash", str(script)],
        cwd=packit_dir,
        env=env,
        callback=callback,
        cancel_event=cancel_event,
    )

    images = sorted(
        path
        for path in (packit_dir / "output").glob("*.img")
        if path.is_file() and path.stat().st_size > 0
    )
    if not images:
        raise ArmPackagerError(f"packit produced no non-empty .img files for {device}")
    archive_output = output_dir / f"openwrt_{device}_{kernel_version}.7z"
    _run(
        ["7z", "a", "-mx=9", str(archive_output), *[str(path) for path in images]],
        cwd=packit_dir / "output",
        env=env,
        callback=callback,
        cancel_event=cancel_event,
    )
    if not archive_output.is_file() or archive_output.stat().st_size <= 0:
        raise ArmPackagerError(f"ARM archive was not created: {archive_output}")
    return ArmPackageResult((archive_output,))


__all__ = ["ArmPackagerError", "ArmPackageResult", "package_arm"]
