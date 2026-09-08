from owrt_builder.devices import load_catalog
from owrt_builder.web import _fragment_defaults, public_catalog, validate_options


def test_package_local_option_requires_selected_owner_even_without_prefix() -> None:
    items = [
        {
            "name": "tool-a",
            "symbol": "CONFIG_PACKAGE_tool-a",
            "options": [
                {"symbol": "CONFIG_TOOL_A_MODE", "kind": "bool", "package": "tool-a"},
            ],
        },
        {"name": "luci-app-demo", "symbol": "CONFIG_PACKAGE_luci-app-demo", "is_plugin": True, "options": []},
    ]

    issues = validate_options(items, ["luci-app-demo"], {"CONFIG_TOOL_A_MODE": True})
    assert any(issue["kind"] == "option_unselected_package" for issue in issues)

    issues = validate_options(items, ["tool-a"], {"CONFIG_TOOL_A_MODE": True})
    assert not any(issue["kind"] == "option_unselected_package" for issue in issues)

    # API callers may use the package Kconfig symbol and the short option
    # symbol; both spellings still resolve to the same catalog owner.
    issues = validate_options(items, ["CONFIG_PACKAGE_tool-a"], {"TOOL_A_MODE": True})
    assert not any(issue["kind"] == "option_unselected_package" for issue in issues)


def test_public_catalog_keeps_plugins_and_reviewed_non_luci_defaults() -> None:
    items = [
        {"name": "luci-app-demo", "symbol": "CONFIG_PACKAGE_luci-app-demo", "is_plugin": True},
        {"name": "luci-theme-demo", "symbol": "CONFIG_PACKAGE_luci-theme-demo", "is_plugin": True},
        {"name": "tailscale", "symbol": "CONFIG_PACKAGE_tailscale"},
        {"name": "kmod-ethernet", "symbol": "CONFIG_PACKAGE_kmod-ethernet"},
    ]

    visible = public_catalog(items, ["tailscale"])
    assert {item["name"] for item in visible} == {"luci-app-demo", "luci-theme-demo", "tailscale"}
    assert {item["category"] for item in visible} == {"luci-app", "luci-theme", "other"}


def test_fragment_defaults_resolves_nonprefixed_package_options() -> None:
    devices = load_catalog()
    runtime = type("RuntimeStub", (), {"devices": devices})()
    spec = devices.resolve("s905d")
    items = [
        {
            "name": "parted",
            "symbol": "CONFIG_PACKAGE_parted",
            "options": [
                {"symbol": "CONFIG_PARTED_READLINE", "package": "parted", "kind": "bool"},
            ],
        },
        {
            "name": "luci-app-demo",
            "symbol": "CONFIG_PACKAGE_luci-app-demo",
            "options": [],
        },
    ]

    packages, options = _fragment_defaults(runtime, spec, items)
    assert "parted" in packages
    assert options["CONFIG_PARTED_READLINE"] is True

    # An orphan package-local symbol from a stale fragment must not make the
    # Web defaults silently select or configure its owner.
    orphan_items = [
        {
            "name": "zabbix",
            "symbol": "CONFIG_PACKAGE_zabbix",
            "options": [
                {"symbol": "CONFIG_ZABBIX_POSTGRESQL", "package": "zabbix", "kind": "bool"},
            ],
        },
    ]
    orphan_packages, orphan_options = _fragment_defaults(runtime, spec, orphan_items)
    assert "zabbix" not in orphan_packages
    assert "CONFIG_ZABBIX_POSTGRESQL" not in orphan_options
