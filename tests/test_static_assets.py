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
