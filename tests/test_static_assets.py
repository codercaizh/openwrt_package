from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mobile_catalog_has_all_category_contracts() -> None:
    html = (ROOT / "owrt_builder/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")
    style = (ROOT / "owrt_builder/static/style.css").read_text(encoding="utf-8")

    for category in ("luci-app", "luci-theme", "other", "all"):
        assert f'data-category="{category}"' in html
    # The browser must apply the server-provided category.  A regression that
    # only checks the package name makes the “默认工具” tab render everything.
    assert 'if (state.category === "all") return true;' in script
    assert "return category === state.category;" in script
    assert "来源 ${origin}" in script
    assert 'id="parallel-jobs"' in html
    assert 'id="reuse-cache"' in html
    assert 'parallel_jobs: parallelJobs' in script
    assert 'reuse_cache: $("reuse-cache").checked' in script
    assert "@media (max-width:560px)" in style


def test_catalog_selection_order_is_stable_until_the_next_catalog_refresh() -> None:
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")

    # Keep the server's order as the source of truth.  The selected-first
    # partition is applied when a catalog/search/device/category refresh
    # happens, so changing a checkbox does not move the card under the user's
    # finger while they are editing it.
    assert "catalogBase: []" in script
    assert "state.catalogBase = (result.items || []).slice();" in script
    assert "const selected = [];" in script
    assert "const unselected = [];" in script
    assert "state.selected.has(catalogItemName(item)) ? selected : unselected" in script
    assert "state.catalog = selected.concat(unselected);" in script
    assert "orderCatalogSelectedFirst();\n    renderCatalog();" in script
    assert "state.category = tab.dataset.category;\n    orderCatalogSelectedFirst();\n    renderCatalog();" in script

    checkbox_start = script.index('checkbox.addEventListener("change"')
    checkbox_end = script.index("\n\n      const body", checkbox_start)
    assert "orderCatalogSelectedFirst();" not in script[checkbox_start:checkbox_end]


def test_cancel_ui_shows_canceling_and_polls_until_terminal_status() -> None:
    html = (ROOT / "owrt_builder/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")
    style = (ROOT / "owrt_builder/static/style.css").read_text(encoding="utf-8")

    assert 'id="cancel-job"' in html
    assert 'canceling: "取消中"' in script
    assert "cancel_requested" in script
    assert "cancelingJobs: new Set()" in script
    assert "cancelPolls: new Map()" in script
    assert "startCancelPolling(id);" in script
    assert '$("cancel-job").hidden = true;' in script
    assert '$("cancel-job").disabled = true;' in script
    assert "if (isTerminalJob(job))" in script
    assert "window.setTimeout(poll, 1000)" in script
    assert ".badge.canceling" in style
