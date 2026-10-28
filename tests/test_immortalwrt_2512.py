from pathlib import Path

from owrt_builder.devices import load_catalog
from owrt_builder.feeds import FEED_SPECS


ROOT = Path(__file__).resolve().parents[1]


def test_all_profiles_use_immutable_official_2512_tag() -> None:
    catalog = load_catalog(ROOT / "configs/devices.toml")
    for device in catalog.devices.values():
        source = catalog.source_for(device)
        assert source.url == "https://github.com/immortalwrt/immortalwrt.git"
        assert source.branch == "v25.12.2"
        assert source.platform == device.platform


def test_pushbot_in_arm_and_router_configs() -> None:
    for name in ("armv8", "n60pro", "360t7"):
        config = (ROOT / "configs" / f"{name}.config").read_text()
        assert "CONFIG_PACKAGE_luci-app-pushbot=y" in config
        assert "CONFIG_PACKAGE_luci-app-passwall_Nftables_Transparent_Proxy=y" in config
        assert "CONFIG_PACKAGE_luci-app-passwall_Iptables_Transparent_Proxy=y" not in config


def test_no_legacy_hnat_patch_with_upgraded_go_toolchain() -> None:
    assert next(source for source in FEED_SPECS if source.name == "golang").branch == "27.x"
    assert not (ROOT / "scripts/iptv-hnat").exists()


def test_mediatek_packager_collects_official_fit_itb(tmp_path: Path, monkeypatch) -> None:
    from owrt_builder.build import BuildEngine
    from owrt_builder.sources import PreparedSource

    engine = BuildEngine(ROOT, tmp_path / "work", use_docker=False)
    target = tmp_path / "source/bin/targets/mediatek/filogic"
    target.mkdir(parents=True)
    image = target / "immortalwrt-25.12.2-mediatek-filogic-netcore_n60-pro-squashfs-sysupgrade.itb"
    image.write_bytes(b"fit-image")
    (target / "immortalwrt-netcore_n60-pro-recovery.itb").write_bytes(b"recovery")
    output = tmp_path / "out"
    output.mkdir()
    prepared = PreparedSource(
        "immortalwrt-25.12-mediatek", "test", tmp_path / "source",
        tmp_path / "catalog.json", "sha"
    )
    monkeypatch.setattr(
        BuildEngine,
        "_firmware_supported_devices",
        staticmethod(lambda *_: ["netcore,n60-pro"]),
    )
    results = engine._package_mediatek(
        engine.catalog.resolve("n60pro"), prepared, tmp_path / "source", output,
        lambda *_: None, None
    )
    assert [x.name for x in results] == [image.name]
    assert (output / image.name).read_bytes() == b"fit-image"


def test_360t7_tag_has_expected_device_profile() -> None:
    catalog = load_catalog(ROOT / "configs/devices.toml")
    assert catalog.resolve("360t7").profile == "qihoo_360t7"
    assert catalog.resolve("n60pro").profile == "netcore_n60-pro"


def test_official_2512_does_not_request_237_append_profiles() -> None:
    from owrt_builder.configuration import compose_fragment
    # Official release has no vendor defconfig/mt7975-ipailna-high-power.
    for name in ("n60pro", "360t7"):
        text = (ROOT / "configs" / f"{name}.config").read_text()
        assert "#CONFIG_APPEND=" not in text
        assert compose_fragment(ROOT / "configs" / f"{name}.config") == text
