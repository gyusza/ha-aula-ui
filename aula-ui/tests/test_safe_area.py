"""Keep iOS safe-area behavior wired into every installed-app page."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "aula_ui" / "static"


def test_pwa_pages_opt_into_safe_area_layout():
    for name in ("index.html", "account.html", "offline.html"):
        assert "viewport-fit=cover" in (ROOT / name).read_text()
    css = (ROOT / "style.css").read_text()
    assert "padding-top:var(--safe-area-top,env(safe-area-inset-top,0px))" in css
    assert "padding:calc(24px + var(--safe-area-top,env(safe-area-inset-top,0px)))" in css
    assert 'const VERSION = "v1"' not in (ROOT / "sw.js").read_text()


def test_pwa_pages_disable_page_pinch_zoom_without_disabling_scroll():
    for name in ("index.html", "account.html", "offline.html"):
        html = (ROOT / name).read_text()
        assert "maximum-scale=1, user-scalable=no" in html
        assert 'src="theme.js"' in html
    assert "touch-action:pan-x pan-y" in (ROOT / "style.css").read_text()
