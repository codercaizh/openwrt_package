"""Device config includes, Kconfig selections and optimization regressions."""
from pathlib import Path

import pytest

from owrt_builder.configuration import ConfigurationError, compose_fragment, parse_config
from owrt_builder.devices import load_catalog


ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"


def test_arm_devices_share_packages_but_not_amlogic_ui() -> None:
    catalog = load_catalog(CONFIGS / "devices.toml")
    assert catalog.resolve("s905d").config == "s905d.config"
    assert catalog.resolve("vplus").config == "vplus.config"
    for name, wants_amlogic in (("s905d", True), ("vplus", False)):
        data = parse_config(compose_fragment(CONFIGS / f"{name}.config"))
        assert data["CONFIG_TARGET_armsr_armv8_DEVICE_generic"] == "y"
        assert data["CONFIG_TARGET_ROOTFS_TARGZ"] == "y"
        assert data["CONFIG_TARGET_ROOTFS_EXT4FS"] == "n"
        if wants_amlogic:
            assert data["CONFIG_PACKAGE_luci-app-amlogic"] == "y"
        else:
            assert "CONFIG_PACKAGE_luci-app-amlogic" not in data
        assert "CONFIG_PACKAGE_kmod-fs-btrfs" not in data
        assert "CONFIG_TARGET_ROOTFS_PARTSIZE" not in data


def test_360t7_preserves_filogic_profile_and_adds_monitoring() -> None:
    data = parse_config(compose_fragment(CONFIGS / "360t7.config"))
    assert data["CONFIG_TARGET_mediatek_filogic_DEVICE_qihoo_360t7"] == "y"
    assert "CONFIG_TARGET_DEVICE_mediatek_filogic_DEVICE_qihoo_360t7" not in data
    assert data["CONFIG_PACKAGE_luci-app-nlbwmon"] == "y"
    assert data["CONFIG_PACKAGE_luci-app-watchcat"] == "y"
    assert data["CONFIG_PACKAGE_luci-app-passwall_INCLUDE_SingBox"] == "y"


def test_reject_traversal_and_include_cycles(tmp_path: Path) -> None:
    bad = tmp_path / "bad.config"
    bad.write_text("#CONFIG_INCLUDE=../other.config\n")
    with pytest.raises(ConfigurationError, match="invalid CONFIG_INCLUDE"):
        compose_fragment(bad)
    a = tmp_path / "a.config"
    b = tmp_path / "b.config"
    a.write_text("#CONFIG_INCLUDE=b.config\n")
    b.write_text("#CONFIG_INCLUDE=a.config\n")
    with pytest.raises(ConfigurationError, match="cyclic CONFIG_INCLUDE"):
        compose_fragment(a)


def test_missing_include_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "missing.config"
    path.write_text("#CONFIG_INCLUDE=not-here.config\n")
    with pytest.raises(ConfigurationError, match="included config not found"):
        compose_fragment(path)


def test_web_defaults_expand_shared_device_profile() -> None:
    from owrt_builder.web import _fragment_defaults

    devices = load_catalog(CONFIGS / "devices.toml")
    runtime = type("RuntimeStub", (), {"devices": devices})()
    catalog = [
        {"name": name, "symbol": f"CONFIG_PACKAGE_{name}", "options": []}
        for name in ("parted", "luci-app-passwall", "luci-app-amlogic")
    ]
    for device, has_amlogic in (("s905d", True), ("vplus", False)):
        packages, _ = _fragment_defaults(runtime, devices.resolve(device), catalog)
        assert "parted" in packages
        assert "luci-app-passwall" in packages
        assert ("luci-app-amlogic" in packages) is has_amlogic

@pytest.mark.parametrize("device", ["360t7", "n60pro", "armv8", "s905d", "vplus"])
def test_all_firmware_configs_include_packet_capture_and_network_diagnostics(device: str) -> None:
    data = parse_config(compose_fragment(CONFIGS / f"{device}.config"))
    for package in ("tcpdump", "iftop", "ethtool", "conntrack", "mtr-nojson", "ip-bridge"):
        assert data[f"CONFIG_PACKAGE_{package}"] == "y", (device, package)
    # The full tcpdump variant provides the capture binary; avoid bundling mini too.
    assert data.get("CONFIG_PACKAGE_tcpdump-mini") != "y"
