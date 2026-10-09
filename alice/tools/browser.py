"""Playwright ile tarayıcı kontrolü.

Profil ~/.alice/browser-profile'da kalıcı, yani bir kere giriş yaptığın siteler
sonraki oturumlarda da açık kalır. Sayfa, pixel yerine ID'li etkileşimli öğe
listesine (DOM snapshot) çevrilir; hem beyin hem reflex döngüsü bunu kullanır.
"""

from __future__ import annotations

from typing import Any

from alice.config import alice_home
from alice.tools.base import COMPUTER, NETWORK, READ, Tool, ToolContext, ToolError, ToolResult, obj, truncate

SNAPSHOT_JS = r"""
(limit) => {
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,summary,[role=button],[role=link],' +
              '[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=combobox],[role=textbox],' +
              '[role=searchbox],[contenteditable=true],[onclick],[tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-alice-id]').forEach(e => e.removeAttribute('data-alice-id'));
  const out = [];
  const vh = window.innerHeight, vw = window.innerWidth;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    if (r.bottom < 0 || r.top > vh * 2.5 || r.right < 0 || r.left > vw) continue;
    const tag = el.tagName.toLowerCase();
    let text = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                el.title || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
    if (!text && tag === 'a') text = el.getAttribute('href') || '';
    if (!text && tag !== 'input' && tag !== 'textarea' && tag !== 'select') continue;
    const id = out.length + 1;
    el.setAttribute('data-alice-id', String(id));
    out.push({
      id, tag, role: el.getAttribute('role') || '', type: el.type || '',
      text: text.slice(0, 90),
      placeholder: (el.placeholder || '').slice(0, 40),
      value: (tag === 'input' || tag === 'textarea') && el.type !== 'password' ? (el.value || '').slice(0, 60) : '',
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : '',
      inView: r.top >= 0 && r.bottom <= vh,
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""

TEXT_JS = "() => document.body ? document.body.innerText : ''"


class BrowserSession:
    def __init__(self, headless: bool = False, executable: str | None = None):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ToolError("playwright kurulu değil: pip install 'alice-harness[browser]' && playwright install chromium") from e
        self._pw = sync_playwright().start()
        profile = alice_home() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"headless": headless, "viewport": None if not headless else {"width": 1366, "height": 860},
                                  "args": ["--start-maximized"]}
        if executable:
            self.ctx = self._pw.chromium.launch_persistent_context(str(profile), executable_path=executable, **kwargs)
        else:
            self.ctx = self._launch(profile, kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.ctx.on("page", self._on_page)

    def _launch(self, profile, kwargs):
        try:
            # Sistemde kurulu Chrome/Edge varsa onu kullan (ayrı indirme gerekmez).
            return self._pw.chromium.launch_persistent_context(str(profile), channel="chrome", **kwargs)
        except Exception:  # noqa: BLE001
            try:
                return self._pw.chromium.launch_persistent_context(str(profile), channel="msedge", **kwargs)
            except Exception:  # noqa: BLE001
                return self._pw.chromium.launch_persistent_context(str(profile), **kwargs)

    def _on_page(self, page) -> None:
        self.page = page  # yeni sekme açılınca ona geç

    def snapshot(self, limit: int = 120) -> list[dict[str, Any]]:
        return self.page.evaluate(SNAPSHOT_JS, limit)

    def locator(self, element_id: int):
        loc = self.page.locator(f'[data-alice-id="{int(element_id)}"]')
        if loc.count() == 0:
            raise ToolError("öğe bulunamadı; sayfa değişmiş olabilir, browser action=snapshot ile yenile")
        return loc.first

    def close(self) -> None:
        try:
            self.ctx.close()
        finally:
            self._pw.stop()


def get_browser(ctx: ToolContext) -> BrowserSession:
    b = ctx.state.get("browser")
    if b is None:
        a = ctx.config.agent
        b = BrowserSession(headless=bool(a.get("browser_headless", False)), executable=a.get("browser_executable") or None)
        ctx.state["browser"] = b
    return b


def format_snapshot(page_title: str, url: str, items: list[dict[str, Any]]) -> str:
    rows = [f"sayfa: {page_title}", f"url: {url}"]
    for i in items:
        label = i["text"] or i["placeholder"] or i["href"]
        kind = i["role"] or i["tag"]
        if i["tag"] == "input" and i["type"]:
            kind = f"input[{i['type']}]"
        extra = f" değer='{i['value']}'" if i["value"] else ""
        off = "" if i["inView"] else " (aşağıda)"
        rows.append(f"[{i['id']}] {kind} '{label}'{extra}{off}")
    return "\n".join(rows)


class Browser(Tool):
    name = "browser"
    description = (
        "Gerçek tarayıcıyı kontrol et (kalıcı profil, girişler hatırlanır). Aksiyonlar:\n"
        "goto(url) | snapshot: tıklanabilir öğeleri ID'leriyle listeler | click(element_id) | "
        "type(element_id, text, submit?) | select(element_id, text) | press(keys, ör. Enter) | "
        "scroll(amount, + aşağı) | back | text: sayfanın düz metni | screenshot | eval(js) | tabs | switch_tab(index) | close.\n"
        "click/type/goto sonrası güncel snapshot otomatik döner."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["goto", "snapshot", "click", "type", "select", "press", "scroll", "back",
                                              "text", "screenshot", "eval", "tabs", "switch_tab", "close"]},
        "url": {"type": "string"},
        "element_id": {"type": "integer"},
        "text": {"type": "string"},
        "submit": {"type": "boolean"},
        "keys": {"type": "string"},
        "amount": {"type": "integer"},
        "js": {"type": "string"},
        "index": {"type": "integer"},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a in ("snapshot", "text", "screenshot", "tabs"):
            return READ
        if a == "goto":
            return NETWORK
        return COMPUTER

    def run(self, ctx: ToolContext, action: str, url: str | None = None, element_id: int | None = None,
            text: str | None = None, submit: bool = False, keys: str | None = None, amount: int = 600,
            js: str | None = None, index: int | None = None) -> ToolResult:
        if action == "close":
            b = ctx.state.pop("browser", None)
            if b:
                b.close()
            return ToolResult("tarayıcı kapatıldı")
        b = get_browser(ctx)
        page = b.page
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))

        def snap(prefix: str) -> ToolResult:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            p = b.page
            return ToolResult(truncate(f"{prefix}\n{format_snapshot(p.title(), p.url, b.snapshot())}", limit))

        if action == "goto":
            if not url:
                raise ToolError("goto için url gerekli")
            if "://" not in url:
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return snap(f"açıldı: {url}")
        if action == "snapshot":
            return snap("snapshot")
        if action == "click":
            loc = b.locator(element_id if element_id is not None else -1)
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=8000)
            page.wait_for_timeout(600)
            return snap(f"tıklandı: [{element_id}]")
        if action == "type":
            loc = b.locator(element_id if element_id is not None else -1)
            loc.fill(text or "", timeout=8000)
            if submit:
                loc.press("Enter")
                page.wait_for_timeout(1000)
            return snap(f"yazıldı: [{element_id}]")
        if action == "select":
            b.locator(element_id if element_id is not None else -1).select_option(label=text, timeout=8000)
            return snap(f"seçildi: {text}")
        if action == "press":
            page.keyboard.press(keys or "Enter")
            page.wait_for_timeout(500)
            return snap(f"tuş: {keys}")
        if action == "scroll":
            page.mouse.wheel(0, amount)
            page.wait_for_timeout(400)
            return snap(f"kaydırıldı: {amount}")
        if action == "back":
            page.go_back(wait_until="domcontentloaded")
            return snap("geri")
        if action == "text":
            return ToolResult(truncate(f"{page.title()}\n{page.url}\n\n{page.evaluate(TEXT_JS)}", limit))
        if action == "screenshot":
            png = page.screenshot(type="png")
            from io import BytesIO

            from PIL import Image

            from alice.tools.imaging import image_to_png_b64

            b64, _ = image_to_png_b64(Image.open(BytesIO(png)), int(ctx.config.agent.get("screenshot_max_width", 1366)))
            return ToolResult(f"tarayıcı görüntüsü: {page.url}", images=[b64])
        if action == "eval":
            if not js:
                raise ToolError("eval için js gerekli")
            return ToolResult(truncate(repr(page.evaluate(js)), limit))
        if action == "tabs":
            rows = [f"{i}: {p.title()} - {p.url}{'  <- aktif' if p is b.page else ''}" for i, p in enumerate(b.ctx.pages)]
            return ToolResult("\n".join(rows))
        if action == "switch_tab":
            pages = b.ctx.pages
            if index is None or not 0 <= index < len(pages):
                raise ToolError("geçersiz sekme index")
            b.page = pages[index]
            b.page.bring_to_front()
            return snap(f"sekme {index}")
        raise ToolError(f"bilinmeyen aksiyon {action}")


def cleanup(ctx: ToolContext) -> None:
    b = ctx.state.pop("browser", None)
    if b:
        try:
            b.close()
        except Exception:  # noqa: BLE001
            pass


TOOLS = [Browser()]
