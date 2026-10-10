"""alice/tools/web.py için birim testleri: httpx.get/post sahte fonksiyonlarla değiştirilir, ağa çıkılmaz."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

from alice.tools import web
from alice.tools.base import NETWORK, ToolContext, ToolError
from tests.conftest import make_config


@pytest.fixture
def ctx(workdir):
    return ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}))


def _resp(url, text, status=200, ctype="text/html; charset=utf-8", method="GET"):
    return httpx.Response(status, headers={"content-type": ctype} if ctype else {}, text=text,
                          request=httpx.Request(method, url))


# ------------------------------------------------------------------ html_to_text


def test_html_to_text_structure():
    raw = ("<html><head><title>T</title><style>x{}</style></head><body><h1>Başlık</h1><h3>Alt</h3>"
           "<p>a &amp; b</p><ul><li>bir</li><li>iki</li></ul>"
           "<a href=\"https://x.com\">link</a> <a href=\"/rel\">rel</a>"
           "<script>alert(1)</script><nav>menü</nav><footer>alt bilgi</footer></body></html>")
    out = web.html_to_text(raw)
    assert out.startswith("# Başlık")
    assert "### Alt" in out
    assert "a & b" in out
    assert "* bir" in out and "* iki" in out
    assert "link (https://x.com)" in out
    assert "(/rel)" not in out  # göreli linkler eklenmez
    for hidden in ("T", "x{}", "alert", "menü", "alt bilgi"):
        assert hidden not in out.split()


def test_html_to_text_whitespace_collapse():
    assert web.html_to_text("<p>a   \t b</p>\n\n\n\n<p>c</p>") == "a b\n\nc"
    assert web.html_to_text("   ") == ""


def test_html_to_text_nested_skip_and_stray_end_tag():
    # iç içe atlanan etiketler sayaçla takip edilir; eşleşmeyen kapanış sayaç negatife düşürmez
    assert web.html_to_text("<head><script>a</script>b</head>c") == "c"
    assert web.html_to_text("</script>görünür") == "görünür"


def test_html_to_text_link_with_nested_markup():
    assert web.html_to_text('<a href="https://a.b">x<b>y</b></a>') == "xy (https://a.b)"


# ------------------------------------------------------------------ WebFetch


def test_web_fetch_meta():
    t = web.WebFetch()
    assert t.risk == NETWORK and t.parallel_safe
    assert t.summary({"url": "https://a"}) == "https://a"
    assert t.summary({}) == ""


def test_web_fetch_adds_scheme_and_converts_html(ctx, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, follow_redirects=False, timeout=None):
        seen.update(url=url, headers=headers, follow_redirects=follow_redirects, timeout=timeout)
        return _resp(url, "<html><body><h1>Merhaba</h1><script>x()</script></body></html>")

    monkeypatch.setattr(web.httpx, "get", fake_get)
    r = web.WebFetch().run(ctx, url="example.com/a")
    assert seen["url"] == "https://example.com/a"
    assert seen["headers"]["User-Agent"] == web.UA and seen["follow_redirects"] is True
    assert r.text == "HTTP 200 https://example.com/a\n\n# Merhaba"
    assert not r.is_error


def test_web_fetch_keeps_explicit_scheme_and_raw(ctx, monkeypatch):
    html_body = "<p>ham</p>"
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, html_body))
    r = web.WebFetch().run(ctx, url="http://x.org", raw=True)
    assert r.text.startswith("HTTP 200 http://x.org")
    assert r.text.endswith(html_body)


def test_web_fetch_non_html_not_converted(ctx, monkeypatch):
    body = '{"a": "<b>"}'
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, body, ctype="application/json"))
    assert web.WebFetch().run(ctx, url="https://api").text.endswith(body)


def test_web_fetch_missing_content_type(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "<p>x</p>", ctype=None))
    assert web.WebFetch().run(ctx, url="https://a").text.endswith("<p>x</p>")


def test_web_fetch_http_error_status(ctx, monkeypatch):
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "yok", status=404, ctype="text/plain"))
    r = web.WebFetch().run(ctx, url="https://a/b")
    assert r.is_error and r.text.startswith("HTTP 404")


def test_web_fetch_network_error_becomes_tool_error(ctx, monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("bağlantı reddedildi")

    monkeypatch.setattr(web.httpx, "get", boom)
    with pytest.raises(ToolError, match="indirilemedi: bağlantı reddedildi"):
        web.WebFetch().run(ctx, url="https://a")


def test_web_fetch_truncates_to_config_limit(workdir, monkeypatch):
    ctx = ToolContext(cwd=workdir, config=make_config({"brain": ["a:m"]}, tool_output_chars=200))
    monkeypatch.setattr(web.httpx, "get", lambda url, **kw: _resp(url, "A" * 5000, ctype="text/plain"))
    out = web.WebFetch().run(ctx, url="https://a").text
    assert "karakter kesildi" in out
    assert len(out) < 400


# ------------------------------------------------------------------ ddg_search / WebSearch


def _result(href, title, snippet=None):
    s = f'<a rel="nofollow" class="result__a" href="{href}">{title}</a>'
    if snippet is not None:
        s += f'<div><a class="result__snippet" href="{href}">{snippet}</a></div>'
    return s


DDG_HTML = "<html><body>" + "".join([
    _result("//duckduckgo.com/l/?uddg=" + quote("https://real.example/a?b=1&c=2", safe="") + "&rut=abc",
            "<b>Gerçek</b> &amp; Sonuç", "Bir <b>özet</b> &quot;tırnak&quot;"),
    _result("https://duckduckgo.com/y.js?ad_provider=x", "Reklam", "reklam metni"),
    _result("https://direct.example/", "Doğrudan"),
    _result("https://third.example/", "Üçüncü", "üç"),
]) + "</body></html>"


@pytest.fixture
def ddg(monkeypatch):
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None, follow_redirects=False):
        seen.update(url=url, data=data, headers=headers)
        return _resp(url, seen.get("html", DDG_HTML), status=seen.get("status", 200), method="POST")

    monkeypatch.setattr(web.httpx, "post", fake_post)
    return seen


def test_ddg_search_parses_results(ddg):
    res = web.ddg_search("python test")
    assert ddg["data"] == {"q": "python test"} and ddg["url"].startswith("https://html.duckduckgo.com")
    assert res == [
        {"title": "Gerçek & Sonuç", "url": "https://real.example/a?b=1&c=2", "snippet": 'Bir özet "tırnak"'},
        {"title": "Doğrudan", "url": "https://direct.example/", "snippet": ""},
        {"title": "Üçüncü", "url": "https://third.example/", "snippet": "üç"},
    ]


def test_ddg_search_max_results(ddg):
    assert [r["title"] for r in web.ddg_search("q", max_results=1)] == ["Gerçek & Sonuç"]


def test_ddg_search_empty_page(ddg):
    ddg["html"] = "<html>sonuç yok</html>"
    assert web.ddg_search("q") == []


def test_ddg_search_http_status_raises(ddg):
    ddg["status"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        web.ddg_search("q")


def test_web_search_formats(ctx, ddg):
    t = web.WebSearch()
    assert t.summary({"query": "abc"}) == "abc"
    out = t.run(ctx, query="q", max_results=2).text
    assert out == ("1. Gerçek & Sonuç\nhttps://real.example/a?b=1&c=2\nBir özet \"tırnak\"\n\n"
                   "2. Doğrudan\nhttps://direct.example/\n")


def test_web_search_no_results(ctx, ddg):
    ddg["html"] = ""
    assert web.WebSearch().run(ctx, query="q").text == "sonuç yok"


def test_web_search_errors_become_tool_error(ctx, ddg, monkeypatch):
    ddg["status"] = 429
    with pytest.raises(ToolError, match="arama başarısız"):
        web.WebSearch().run(ctx, query="q")

    def boom(*a, **k):
        raise httpx.ReadTimeout("zaman aşımı")

    monkeypatch.setattr(web.httpx, "post", boom)
    with pytest.raises(ToolError, match="zaman aşımı"):
        web.WebSearch().run(ctx, query="q")
