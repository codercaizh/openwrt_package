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


def test_lucky_is_not_preinstalled_on_n60pro() -> None:
    n60 = (ROOT / "configs/n60pro.config").read_text()
    assert "CONFIG_PACKAGE_luci-app-lucky=y" not in n60
    assert "CONFIG_PACKAGE_lucky=y" not in n60
    # Other device profiles still request Lucky from the shared upstream feed.
    for name in ("360t7", "armv8"):
        config = (ROOT / "configs" / f"{name}.config").read_text()
        assert "CONFIG_PACKAGE_luci-app-lucky=y" in config


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

def test_ramips_config_generation_uses_native_profile_without_duplicates(tmp_path: Path) -> None:
    from owrt_builder.build import BuildEngine
    from owrt_builder.catalog import Catalog
    from owrt_builder.configuration import parse_config

    engine = BuildEngine(ROOT, tmp_path / "work", use_docker=False)
    source = tmp_path / "source"
    source.mkdir()
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    catalog = Catalog(root=source, packages=[], authoritative=True)
    for device in ("r3g", "k2p", "k2p-ap"):
        spec = engine.catalog.resolve(device)
        generated = engine._write_config(
            spec, engine.catalog.source_for(spec), source, build_dir,
            None, catalog=catalog
        )
        doc = parse_config(generated)
        for symbol in (
            "CONFIG_TARGET_ramips", "CONFIG_TARGET_ramips_mt7621",
            f"CONFIG_TARGET_ramips_mt7621_DEVICE_{spec.profile}",
            "CONFIG_TARGET_PROFILE",
        ):
            assert sum(entry.symbol == symbol for entry in doc.entries) == 1
        assert doc[f"CONFIG_TARGET_ramips_mt7621_DEVICE_{spec.profile}"] == "y"


def test_ramips_packager_collects_only_correct_device_images(tmp_path: Path) -> None:
    from owrt_builder.build import BuildEngine, BuildError

    engine = BuildEngine(ROOT, tmp_path / "work", use_docker=False)
    images = tmp_path / "source/bin/targets/ramips/mt7621"
    images.mkdir(parents=True)
    r3g = images / "immortalwrt-25.12.2-ramips-mt7621-xiaomi_mi-router-3g-squashfs-sysupgrade.bin"
    v2 = images / "immortalwrt-25.12.2-ramips-mt7621-xiaomi_mi-router-3g-v2-squashfs-sysupgrade.bin"
    k2p = images / "immortalwrt-25.12.2-ramips-mt7621-phicomm_k2p-squashfs-sysupgrade.bin"
    recovery = images / "immortalwrt-25.12.2-ramips-mt7621-xiaomi_mi-router-3g-squashfs-kernel1.bin"
    for image in (r3g, v2, k2p, recovery):
        image.write_bytes(b"firmware")
    artifact_dir = tmp_path / "out"
    artifact_dir.mkdir()
    for device, wanted in (
        ("r3g", {r3g.name, recovery.name}),
        ("k2p", {k2p.name.replace("-sysupgrade.bin", "-router-sysupgrade.bin")}),
        ("k2p-ap", {k2p.name.replace("-sysupgrade.bin", "-ap-sysupgrade.bin")}),
    ):
        outputs = engine._package_ramips(
            engine.catalog.resolve(device), tmp_path / "source",
            artifact_dir, lambda *_: None
        )
        assert {item.name for item in outputs} == wanted
    r3g.unlink()
    recovery.unlink()
    with __import__("pytest").raises(BuildError, match="no xiaomi_mi-router-3g"):
        engine._package_ramips(
            engine.catalog.resolve("r3g"), tmp_path / "source",
            artifact_dir, lambda *_: None
        )

def test_k2p_ap_validation_rejects_router_services_after_defconfig(tmp_path: Path) -> None:
    import pytest
    from owrt_builder.build import BuildEngine, BuildError
    from owrt_builder.configuration import parse_config

    engine = BuildEngine(ROOT, tmp_path / "work", use_docker=False)
    k2p = engine.catalog.resolve("k2p-ap")
    router = engine.catalog.resolve("k2p")
    r3g = engine.catalog.resolve("r3g")
    clean = parse_config("CONFIG_PACKAGE_tcpdump-mini=y\nCONFIG_PACKAGE_firewall4=n\n")
    engine._validate_ap_package_exclusions(k2p, clean)
    polluted = parse_config("CONFIG_PACKAGE_luci=y\nCONFIG_PACKAGE_dnsmasq-full=y\n")
    with pytest.raises(BuildError, match="luci, dnsmasq-full"):
        engine._validate_ap_package_exclusions(k2p, polluted)
    # Router variant and other devices retain their full router package policy.
    engine._validate_ap_package_exclusions(router, polluted)
    engine._validate_ap_package_exclusions(r3g, polluted)

def test_k2p_only_stages_ram_xray_overlay(tmp_path: Path) -> None:
    from owrt_builder.build import BuildEngine

    engine = BuildEngine(ROOT, tmp_path / "work", use_docker=False)
    work = tmp_path / "openwrt"
    work.mkdir()
    engine._stage_device_files(engine.catalog.resolve("k2p"), work)
    overlay = work / "files"
    init = overlay / "etc/init.d/k2p-xray-ram"
    rc = overlay / "etc/rc.d/S98k2p-xray-ram"
    binary = overlay / "usr/bin/xray"
    assert init.is_file() and init.stat().st_mode & 0o111
    assert rc.is_symlink()
    assert rc.readlink().as_posix() == "../init.d/k2p-xray-ram"
    assert binary.is_symlink()
    assert binary.readlink().as_posix() == "/tmp/k2p-xray/xray"
    script = init.read_text()
    assert "xray_softfloat" in script
    assert 'XRAY_VERSION="v26.10.21"' in script
    assert 'XRAY_SHA256="1e9953aecee4024d9da6427a42929817b53d1660f5122ec6b22629e11cab4012"' in script
    assert 'sha256sum "$XRAY_ARCHIVE"' in script
    assert "XRAY_BIN=" in script
    assert not any(path.is_file() and path.name == "xray" for path in overlay.rglob("*"))

    other = tmp_path / "other"
    other.mkdir()
    engine._stage_device_files(engine.catalog.resolve("k2p-ap"), other)
    assert not (other / "files").exists()
