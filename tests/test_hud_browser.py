"""Tarayıcıda üzerinde çalışılan öğenin HUD'da çerçevelenmesi: sayfa içi kutunun ekran koordinatına çevrilmesi,
HUD kapalıyken ya da tarayıcı görünmezken hiçbir şey yapılmaması. Sahte sayfa; gerçek tarayıcı açılmaz."""

from __future__ import annotations

import time

import pytest

from alice.hud import client, views
from alice.tools import browser
from alice.tools.base import READ, ToolContext
from tests.conftest import make_config
from tests.test_browser_tool import make_session


def place(**over):
    """Pencere (100, 50)'de, yan çerçeve 8, alt çerçeve 8, sekme+adres çubuğu 80 CSS px; ölçek 1.5."""
    info = {"left": 20, "top": 30, "width": 100, "height": 40, "sx": 100, "sy": 50, "ow": 1216, "oh": 888,
            "iw": 1200, "ih": 800, "dpr": 1.5, "visible": True}
    info.update(over)
    return info


def test_screen_box_converts_page_rect_to_physical_pixels():
    # Sayfanın sol üstü: (100 + 8, 50 + 80) CSS px; öğe +PAD çerçeveyle (16, 26)'dan 108x48.
    assert browser.screen_box(place()) == (round((108 + 16) * 1.5), round((130 + 26) * 1.5), 162, 72)
    # Ekranı kaplayan (çerçevesiz) pencere, ölçek 1.
    assert browser.screen_box(place(sx=0, sy=0, ow=1200, oh=880, dpr=1)) == (16, 106, 108, 48)


def test_screen_box_clips_to_viewport():
    # Yarısı aşağıda kalan öğe: yalnızca görünen kısmı çerçevelenir.
    x, y, w, h = browser.screen_box(place(top=780, dpr=1))
    assert h == 800 - 776 and y == 130 + 776
    assert browser.screen_box(place(top=900)) is None  # tamamen görünür alanın dışında
    assert browser.screen_box(place(left=-500)) is None


@pytest.mark.parametrize("bad", [
    None, [], "x", place(visible=False), place(dpr=0), place(iw=0), place(ow=100),  # pencere içerikten dar: saçma
    place(oh=2000),  # dev "araç çubuğu": ölçüm güvenilmez
    place(dpr=float("nan")), place(left="a"), {"left": 1},
])
def test_screen_box_skips_when_it_cannot_be_computed(bad):
    assert browser.screen_box(bad) is None


@pytest.fixture
def hud_on(monkeypatch):
    sent = []
    monkeypatch.setattr(client, "active", True)
    monkeypatch.setattr(client, "send", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr(views, "CLICK_PREVIEW", 0)
    return sent


def test_page_target_draws_a_box(hud_on):
    assert views.page_target(10, 20, 30, 40, "burada", ttl=6)
    assert hud_on == [{"kind": "box", "ttl": 6, "x": 10, "y": 20, "w": 30, "h": 40, "text": "burada",
                       "severity": "info", "id": "browser"}]
    assert not views.page_target(10, 20, 1, 40)  # boş kutu çizilmez


def test_page_target_does_nothing_when_hud_is_off(monkeypatch):
    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))
    t0 = time.monotonic()
    assert not views.page_target(10, 20, 30, 40, wait=True)
    assert time.monotonic() - t0 < 0.1


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, tool_output_chars=4000))


def _attached(ctx, info):
    s = make_session(attached=True)
    s.page.eval_result = lambda js, *a, **k: info if js == browser.PLACE_JS else []
    ctx.state["browser"] = s
    return s


def test_click_frames_element_before_clicking(ctx, hud_on):
    s = _attached(ctx, place(dpr=1))
    browser.Browser().run(ctx, "click", element_id=3)
    names = [c[0] for c in s.page.calls]
    assert names.index("evaluate") < names.index("click")  # önce çerçeve, sonra tıklama
    assert hud_on[0]["kind"] == "box" and hud_on[0]["text"] == "tıklıyorum" and hud_on[0]["id"] == "browser"
    assert (hud_on[0]["x"], hud_on[0]["y"], hud_on[0]["w"], hud_on[0]["h"]) == (124, 156, 108, 48)


def test_hud_off_never_measures_the_page(ctx, monkeypatch):
    monkeypatch.setattr(client, "active", False)
    monkeypatch.setattr(client, "send", lambda msg: pytest.fail("HUD kapalıyken gönderilmemeli"))
    s = _attached(ctx, place())
    browser.Browser().run(ctx, "click", element_id=3)
    browser.Browser().run(ctx, "type", element_id=3, text="kedi")
    assert not any(c[0] == "evaluate" and c[2][:1] == (browser.PLACE_JS,) for c in s.page.calls)


def test_headless_draws_nothing(ctx, hud_on):
    s = _attached(ctx, place())
    s.headless = True
    browser.Browser().run(ctx, "click", element_id=3)
    out = browser.Browser().run(ctx, "highlight", element_id=3)
    assert hud_on == [] and "headless" in out.text
    measured = [c for c in s.page.calls if c[0] == "evaluate" and c[2][:1] in ((browser.PLACE_JS,), (browser.OUTLINE_JS,))]
    assert measured == []  # ölçülmez, sayfaya da çerçeve eklenmez


def test_unmeasurable_or_failing_page_is_skipped_silently(ctx, hud_on):
    s = _attached(ctx, place(visible=False))  # sekme arka planda
    browser.Browser().run(ctx, "click", element_id=3)

    def boom(js, *a, **k):
        if js == browser.PLACE_JS:
            raise RuntimeError("sayfa cevap vermedi")
        return []

    s.page.eval_result = boom
    browser.Browser().run(ctx, "click", element_id=3)
    assert hud_on == [] and sum(c[0] == "click" for c in s.page.calls) == 2  # tıklama yine yapıldı


def test_highlight_shows_element_with_label(ctx, hud_on):
    assert browser.Browser().risk_for({"action": "highlight"}) == READ
    _attached(ctx, place(dpr=1))
    out = browser.Browser().run(ctx, "highlight", element_id=3, text="Fiyat burada")
    assert "ekranda" in out.text
    assert hud_on[-1]["text"] == "Fiyat burada" and hud_on[-1]["ttl"] == views.SHOW_TTL


def test_highlight_falls_back_to_page_outline_without_hud(ctx, monkeypatch):
    monkeypatch.setattr(client, "active", False)
    s = _attached(ctx, place())
    out = browser.Browser().run(ctx, "highlight", element_id=3)
    assert "sayfada" in out.text
    assert any(c[0] == "evaluate" and c[2][:1] == (browser.OUTLINE_JS,) for c in s.page.calls)
    with pytest.raises(Exception, match="element_id"):
        browser.Browser().run(ctx, "highlight")
