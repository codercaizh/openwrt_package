from __future__ import annotations

from pathlib import Path

from owrt_builder.catalog import Catalog, scan_catalog


def _generated_tree(root: Path) -> None:
    (root / "tmp").mkdir(parents=True)
    (root / "tmp/.packageinfo").write_text(
        """Source-Makefile: package/feeds/packages/menu-demo/Makefile
Package: menu-demo
Section: utils
Category: Utilities
Title: menu demo
Description: A package used by the parser test.
@@
Source-Makefile: package/feeds/packages/plain-demo/Makefile
Package: plain-demo
Section: utils
Title: plain demo
@@
""",
        encoding="utf-8",
    )
    (root / "tmp/.config-package.in").write_text(
        """menu "Image configuration"
\tmenuconfig PACKAGE_menu-demo
\t\ttristate "menu demo"
\t\tconfig DEMO_MODE
\t\t\tbool "Mode"
\t\tconfig DEMO_MODE
\t\t\tbool "Mode duplicate"
\t\tconfig DEMO_PATH
\t\t\tstring "Path"
\tconfig PACKAGE_plain-demo
\t\ttristate "plain demo"
\t\tconfig PLAIN_FEATURE
\t\t\tbool "Feature"
endmenu
""",
        encoding="utf-8",
    )


def test_generated_package_blocks_associate_menuconfig_and_non_prefix_options(tmp_path: Path) -> None:
    _generated_tree(tmp_path)
    catalog = scan_catalog(tmp_path)

    menu_demo = catalog.package("menu-demo")
    assert menu_demo is not None
    assert {option.symbol for option in menu_demo.options} == {
        "CONFIG_DEMO_MODE",
        "CONFIG_DEMO_PATH",
    }
    assert all(option.package == "menu-demo" for option in menu_demo.options)
    assert len(menu_demo.options) == len({option.symbol for option in menu_demo.options})

    plain_demo = catalog.package("plain-demo")
    assert plain_demo is not None
    assert [option.symbol for option in plain_demo.options] == ["CONFIG_PLAIN_FEATURE"]


def test_catalog_json_round_trip_preserves_generated_metadata(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "source"
    _generated_tree(source)
    catalog = scan_catalog(source)
    # Catalog.read resolves the sibling source directory exactly as a real
    # immutable source snapshot does.
    path = catalog.write(snapshot / "catalog.json")

    restored = Catalog.read(path)
    assert restored.to_dict() == catalog.to_dict()
    assert restored.package("menu-demo").options[0].package == "menu-demo"
