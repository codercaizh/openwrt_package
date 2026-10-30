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

@pytest.mark.parametrize("device", ["360t7", "n60pro", "armv8", "s905d", "vplus", "r3g"])
def test_all_firmware_configs_include_packet_capture_and_network_diagnostics(device: str) -> None:
    data = parse_config(compose_fragment(CONFIGS / f"{device}.config"))
    for package in ("tcpdump", "iftop", "ethtool", "conntrack", "mtr-nojson", "ip-bridge"):
        assert data[f"CONFIG_PACKAGE_{package}"] == "y", (device, package)
    # The full tcpdump variant provides the capture binary; avoid bundling mini too.
    assert data.get("CONFIG_PACKAGE_tcpdump-mini") != "y"


@pytest.mark.parametrize(
    ("device", "profile"),
    [("r3g", "xiaomi_mi-router-3g"), ("k2p", "phicomm_k2p"), ("k2p-ap", "phicomm_k2p")],
)
def test_ramips_profiles_have_exact_official_target(device: str, profile: str) -> None:
    catalog = load_catalog(CONFIGS / "devices.toml")
    spec = catalog.resolve(device)
    assert spec.target == "ramips/mt7621"
    assert spec.profile == profile
    assert spec.packager == "ramips"
    data = parse_config(compose_fragment(CONFIGS / spec.config))
    assert data["CONFIG_TARGET_ramips"] == "y"
    assert data["CONFIG_TARGET_ramips_mt7621"] == "y"
    assert data[f"CONFIG_TARGET_ramips_mt7621_DEVICE_{profile}"] == "y"
    assert data["CONFIG_TARGET_PROFILE"] == f'"DEVICE_{profile}"'
    if device == "k2p-ap":
        assert data["CONFIG_PACKAGE_luci"] == "n"
        assert data["CONFIG_PACKAGE_luci-mod-admin-full"] == "y"
        assert "CONFIG_PACKAGE_luci-app-passwall" not in data
    else:
        assert data["CONFIG_PACKAGE_luci"] == "y"
    assert "CONFIG_PACKAGE_tailscale" not in data


def test_k2p_is_a_minimal_ap_with_packet_capture() -> None:
    config = parse_config(compose_fragment(CONFIGS / "k2p-ap.config"))
    for package in (
        "luci-base", "luci-mod-admin-full", "luci-theme-bootstrap",
        "uhttpd", "uhttpd-mod-ubus", "tcpdump-mini",
        "ethtool", "iwinfo", "ip-bridge",
    ):
        assert config[f"CONFIG_PACKAGE_{package}"] == "y", package
    for package in (
        "luci", "dnsmasq-full", "firewall4", "nftables",
        "kmod-nft-offload", "odhcp6c", "odhcpd-ipv6only",
        "ppp", "ppp-mod-pppoe", "luci-app-firewall",
        "luci-proto-ppp", "luci-app-package-manager",
        "default-settings-chn", "block-mount", "kmod-nf-nathelper",
    ):
        assert config[f"CONFIG_PACKAGE_{package}"] == "n", package
    for package in (
        "tcpdump", "iftop", "conntrack", "mtr-nojson", "iperf3",
        "htop", "luci-app-ttyd", "luci-app-passwall", "tailscale",
    ):
        assert config.get(f"CONFIG_PACKAGE_{package}") != "y", package


def test_k2p_router_has_standard_services_and_only_passwall() -> None:
    data = parse_config(compose_fragment(CONFIGS / "k2p.config"))
    assert data["CONFIG_PACKAGE_luci"] == "y"
    assert data["CONFIG_PACKAGE_luci-app-passwall"] == "y"
    assert data["CONFIG_PACKAGE_luci-app-passwall_Nftables_Transparent_Proxy"] == "y"
    assert data["CONFIG_PACKAGE_luci-app-passwall_INCLUDE_ShadowsocksR_Libev_Client"] == "y"
    assert data["CONFIG_PACKAGE_tcpdump-mini"] == "y"
    for core in ("Xray", "SingBox", "Hysteria", "SmartDNS", "V2ray_Geodata"):
        assert data[f"CONFIG_PACKAGE_luci-app-passwall_INCLUDE_{core}"] == "n"
    for pkg in ("luci-app-lucky", "luci-app-pushbot", "luci-app-ttyd", "tailscale"):
        assert data.get(f"CONFIG_PACKAGE_{pkg}", "n") != "y"
    for pkg in ("dnsmasq-full", "firewall4", "nftables", "odhcp6c",
                "odhcpd-ipv6only", "ppp", "ppp-mod-pppoe", "wpad-openssl"):
        assert data[f"CONFIG_PACKAGE_{pkg}"] == "y"


def test_k2p_variants_have_distinct_catalog_keys_and_configs() -> None:
    cat = load_catalog(CONFIGS / "devices.toml")
    router = cat.resolve("k2p")
    ap = cat.resolve("k2p-ap")
    assert router.config == "k2p.config"
    assert ap.config == "k2p-ap.config"
    assert router.profile == ap.profile == "phicomm_k2p"
    assert router.key != ap.key
    assert "ROUTER" in router.description
    assert "AP" in ap.description
