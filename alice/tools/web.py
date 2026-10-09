"""API anahtarı gerektirmeyen web arama ve sayfa okuma."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

from typing import Any

import httpx

from alice.tools.base import NETWORK, Tool, ToolContext, ToolError, ToolResult, obj, truncate

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.parts.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.parts.append("* ")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and self._href and self._href.startswith("http"):
            self.parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


class WebFetch(Tool):
    name = "web_fetch"
    parallel_safe = True
    description = "URL'yi indir, HTML ise okunabilir metne çevir. JS gerektiren sayfalar için browser aracını kullan."
    parameters = obj({"url": {"type": "string"}, "raw": {"type": "boolean", "description": "HTML'i dönüştürmeden döndür"}}, ["url"])
    risk = NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("url", ""))

    def run(self, ctx: ToolContext, url: str, raw: bool = False) -> ToolResult:
        if "://" not in url:
            url = "https://" + url
        try:
            r = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
        except httpx.HTTPError as e:
            raise ToolError(f"indirilemedi: {e}") from e
        ctype = r.headers.get("content-type", "")
        body = r.text
        if not raw and "html" in ctype:
            body = html_to_text(body)
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        return ToolResult(truncate(f"HTTP {r.status_code} {r.url}\n\n{body}", limit), is_error=r.status_code >= 400)


def ddg_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": UA},
                   timeout=20, follow_redirects=True)
    r.raise_for_status()
    results = []
    blocks = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                        r.text, re.S)
    for href, title, rest in blocks:
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        if "duckduckgo.com/y.js" in href:
            continue  # reklam
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
        results.append({"title": clean(title), "url": href, "snippet": clean(snip.group(1)) if snip else ""})
        if len(results) >= max_results:
            break
    return results


class WebSearch(Tool):
    name = "web_search"
    parallel_safe = True
    description = "İnternette ara (DuckDuckGo, anahtar gerekmez). Başlık, URL ve özet döner; detay için web_fetch kullan."
    parameters = obj({"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"])
    risk = NETWORK

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("query", ""))

    def run(self, ctx: ToolContext, query: str, max_results: int = 8) -> ToolResult:
        try:
            res = ddg_search(query, max_results)
        except httpx.HTTPError as e:
            raise ToolError(f"arama başarısız: {e}") from e
        if not res:
            return ToolResult("sonuç yok")
        return ToolResult("\n\n".join(f"{i}. {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(res, 1)))


TOOLS = [WebFetch(), WebSearch()]
