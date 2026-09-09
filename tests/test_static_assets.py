import asyncio
from pathlib import Path
import re

import httpx

from owrt_builder.web import Settings, create_app


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


def test_build_form_submits_directly_without_frontend_validation_button() -> None:
    html = (ROOT / "owrt_builder/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")

    assert 'id="validate"' not in html
    assert "校验配置" not in html
    assert "validateConfiguration" not in script
    assert "/api/configuration/validate" not in script
    assert 'id="submit-job"' in html
    assert 'bindEvent("submit-job", "click", submitJob);' in script
    assert 'api("/api/jobs", { method: "POST", body: configurationBody() })' in script
    # Server-side validation feedback returned by submission remains visible.
    assert "renderIssues(result.issues || []);" in script


def test_index_versions_assets_and_disables_document_caching(tmp_path: Path) -> None:
    app = create_app(settings=Settings(data_dir=tmp_path))

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/")

    response = asyncio.run(request())
    assert response.status_code == 200
    versions = re.findall(r'/static/(?:style\.css|app\.js)\?v=([0-9a-f]{16})', response.text)
    assert len(versions) == 2
    assert len(set(versions)) == 1
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["pragma"] == "no-cache"


def test_frontend_formats_task_times_and_log_rows_as_beijing_time() -> None:
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")

    assert 'new Intl.DateTimeFormat("zh-CN"' in script
    assert 'timeZone: "Asia/Shanghai"' in script
    assert "function formatBeijingTime(value)" in script
    assert "formatBeijingTime(job.created_at)" in script
    assert "formatBeijingTime(row.created_at)" in script
    assert "北京时间" in script


def test_source_badge_uses_last_success_time_and_handles_stale_refreshes() -> None:
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")
    start = script.index("  async function loadSource()")
    end = script.index("\n\n  async function loadCatalog()", start)
    source_loader = script[start:end]

    assert "function formatSourceUpdateTime(source)" in script
    assert "source?.last_success_at || source?.snapshot?.created_at" in script
    assert "源码更新中 · ${updatedAt}" in source_loader
    assert "更新失败 · ${updatedAt}" in source_loader
    assert "源码已就绪 · ${updatedAt}" in source_loader
    assert "当前版本更新时间未知" in script
    # The source badge must no longer expose an opaque snapshot id as its
    # freshness indicator; the catalogue may still show that id elsewhere.
    assert "snapshot_id" not in source_loader


def test_top_level_event_bindings_tolerate_missing_optional_nodes() -> None:
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")

    assert "function bindEvent(id, eventName, listener)" in script
    assert "if (node) node.addEventListener(eventName, listener);" in script
    assert not re.search(r'\$\("[^"]+"\)\.addEventListener', script)
    assert 'bindEvent("submit-job", "click", submitJob);' in script


def test_settings_ui_uses_masked_pushplus_status_and_account_update_api() -> None:
    html = (ROOT / "owrt_builder/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "owrt_builder/static/app.js").read_text(encoding="utf-8")

    for element_id in ("open-settings", "settings-panel", "account-form", "pushplus-form", "pushplus-status", "clear-pushplus"):
        assert f'id="{element_id}"' in html
    assert 'api("/api/settings")' in script
    assert 'api("/api/settings", { method: "PUT", body: { pushplus_token: token } })' in script
    assert 'body: { clear_pushplus: true }' in script
    assert "requires_login" in script
    assert "push-secret" not in script


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
