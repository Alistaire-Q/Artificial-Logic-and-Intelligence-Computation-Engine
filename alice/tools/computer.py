"""Ekran, fare ve klavye kontrolü (pixel seviyesinde bilgisayar kullanımı).

Koordinatlar her zaman SON ekran görüntüsünün (küçültülmüş) piksel uzayındadır;
araç bunları gerçek ekran koordinatına kendisi çevirir. pyautogui FAILSAFE açık:
fareyi ekranın sol üst köşesine götürmek ALICE'in fare/klavye hareketini anında keser.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

_DPI_SET = False


def _dpi_aware() -> None:
    """Windows'ta ölçekleme (%125/%150) varken mss ile pyautogui'nin aynı pikselleri görmesi için."""
    global _DPI_SET
    if _DPI_SET or sys.platform != "win32":
        return
    _DPI_SET = True
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _pyautogui():
    _dpi_aware()
    try:
        import pyautogui
    except ImportError as e:
        raise ToolError("pyautogui kurulu değil: pip install 'alice-harness[computer]'") from e
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def grab_screen(ctx: ToolContext, grid: bool = False, monitor: int = 1) -> tuple[str, str]:
    """Ekran görüntüsü al, ölçeği ctx.state'e kaydet. (b64 png, açıklama) döner."""
    _dpi_aware()
    try:
        import mss
    except ImportError as e:
        raise ToolError("mss kurulu değil: pip install 'alice-harness[computer]'") from e
    from PIL import Image, ImageDraw

    from alice.tools.imaging import image_to_png_b64

    with mss.mss() as sct:
        mons = sct.monitors
        if monitor >= len(mons):
            raise ToolError(f"monitör {monitor} yok, {len(mons) - 1} monitör var")
        mon = mons[monitor]
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    max_w = int(ctx.config.agent.get("screenshot_max_width", 1366))
    scale = img.width / max_w if img.width > max_w else 1.0
    if grid:
        # Izgara, küçültülmüş uzayda her 100 pikselde bir etiketlenecek şekilde orijinale çizilir.
        d = ImageDraw.Draw(img)
        step = 100 * scale
        x = step
        while x < img.width:
            d.line([(x, 0), (x, img.height)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((x + 2, 2), str(int(round(x / scale))), fill=(255, 0, 255))
            x += step
        y = step
        while y < img.height:
            d.line([(0, y), (img.width, y)], fill=(255, 0, 255), width=max(1, int(scale)))
            d.text((2, y + 2), str(int(round(y / scale))), fill=(255, 0, 255))
            y += step
    b64, scale = image_to_png_b64(img, max_w)
    ctx.state["screen"] = {"scale": scale, "left": mon["left"], "top": mon["top"],
                           "w": round(img.width / scale), "h": round(img.height / scale)}
    s = ctx.state["screen"]
    return b64, f"ekran görüntüsü {s['w']}x{s['h']} (gerçek {img.width}x{img.height}, monitör {monitor})"


def to_real(ctx: ToolContext, x: float, y: float) -> tuple[int, int]:
    s = ctx.state.get("screen")
    if not s:
        raise ToolError("önce action=screenshot al; koordinatlar ekran görüntüsüne göre verilir")
    return int(s["left"] + x * s["scale"]), int(s["top"] + y * s["scale"])


def type_text(text: str) -> None:
    pg = _pyautogui()
    if text.isascii():
        pg.write(text, interval=0.01)
        return
    # Türkçe karakterler pyautogui.write ile yazılamıyor; panodan yapıştır.
    try:
        import pyperclip
    except ImportError as e:
        raise ToolError("ASCII olmayan metin için pyperclip gerekli") from e
    old = None
    try:
        old = pyperclip.paste()
    except Exception:  # noqa: BLE001
        pass
    pyperclip.copy(text)
    pg.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
    time.sleep(0.15)
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:  # noqa: BLE001
            pass


def press_keys(keys: str) -> None:
    pg = _pyautogui()
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    alias = {"control": "ctrl", "cmd": "command", "win": "win", "windows": "win", "return": "enter",
             "esc": "escape", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}
    parts = [alias.get(p, p) for p in parts]
    if len(parts) == 1:
        pg.press(parts[0])
    else:
        pg.hotkey(*parts)


class Computer(Tool):
    name = "computer"
    description = (
        "Ekranı gör ve fare/klavyeyi kullan. Önce action=screenshot al; x,y koordinatları o görüntünün "
        "piksel uzayındadır. grid=true koordinat ızgarası çizer (hassas tıklama için). "
        "Mümkünse önce ui_elements/browser araçlarını tercih et, pixel tıklama son çare olsun. "
        "Aksiyonlar: screenshot, click, double_click, right_click, middle_click, move, drag, scroll, "
        "type, key (ör. 'ctrl+s', 'alt+tab', 'enter'), wait, cursor_position."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["screenshot", "click", "double_click", "right_click", "middle_click",
                                              "move", "drag", "scroll", "type", "key", "wait", "cursor_position"]},
        "x": {"type": "number"}, "y": {"type": "number"},
        "to_x": {"type": "number"}, "to_y": {"type": "number"},
        "text": {"type": "string", "description": "type için metin"},
        "keys": {"type": "string", "description": "key için kombinasyon, ör. ctrl+shift+esc"},
        "amount": {"type": "integer", "description": "scroll: pozitif yukarı, negatif aşağı (tık sayısı)"},
        "seconds": {"type": "number"},
        "grid": {"type": "boolean"},
        "monitor": {"type": "integer", "description": "1 = ana monitör, 2 = ikinci, 0 = hepsi yan yana"},
        "screenshot_after": {"type": "boolean", "description": "aksiyondan sonra yeni ekran görüntüsü döndür"},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("screenshot", "cursor_position", "wait") else COMPUTER

    def run(self, ctx: ToolContext, action: str, x: float | None = None, y: float | None = None,
            to_x: float | None = None, to_y: float | None = None, text: str | None = None,
            keys: str | None = None, amount: int = -5, seconds: float = 1.0, grid: bool = False,
            monitor: int = 1, screenshot_after: bool = False) -> ToolResult:
        if action == "screenshot":
            b64, desc = grab_screen(ctx, grid=grid, monitor=monitor)
            return ToolResult(desc, images=[b64])
        if action == "wait":
            time.sleep(max(0.0, min(float(seconds), 30.0)))
            msg = f"{seconds} sn beklendi"
        elif action == "cursor_position":
            pg = _pyautogui()
            px, py = pg.position()
            s = ctx.state.get("screen") or {"scale": 1.0, "left": 0, "top": 0}
            return ToolResult(f"imleç: ({(px - s['left']) / s['scale']:.0f}, {(py - s['top']) / s['scale']:.0f}) ekran görüntüsü uzayında")
        elif action == "type":
            if text is None:
                raise ToolError("type için text gerekli")
            type_text(text)
            msg = f"{len(text)} karakter yazıldı"
        elif action == "key":
            if not keys:
                raise ToolError("key için keys gerekli")
            press_keys(keys)
            msg = f"tuş: {keys}"
        else:
            pg = _pyautogui()
            if x is None or y is None:
                raise ToolError(f"{action} için x ve y gerekli")
            rx, ry = to_real(ctx, x, y)
            if action in ("click", "double_click", "right_click", "middle_click"):
                button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
                pg.click(rx, ry, clicks=2 if action == "double_click" else 1, button=button, interval=0.08)
            elif action == "move":
                pg.moveTo(rx, ry, duration=0.1)
            elif action == "drag":
                if to_x is None or to_y is None:
                    raise ToolError("drag için to_x ve to_y gerekli")
                tx, ty = to_real(ctx, to_x, to_y)
                pg.moveTo(rx, ry)
                pg.dragTo(tx, ty, duration=0.4, button="left")
            elif action == "scroll":
                pg.moveTo(rx, ry)
                # Windows'ta birim wheel delta (120 = bir çentik), X11'de çentik, macOS'ta satır.
                unit = {"win32": 120, "darwin": 5}.get(sys.platform, 1)
                pg.scroll(int(amount) * unit)
            msg = f"{action} ({x:.0f},{y:.0f})"
        if screenshot_after:
            time.sleep(0.4)
            b64, desc = grab_screen(ctx, monitor=monitor)
            return ToolResult(f"{msg}\n{desc}", images=[b64])
        return ToolResult(msg)


class ComputerTask(Tool):
    name = "computer_task"
    description = (
        "Bilgisayarı kullanıcı gibi kullanarak çok adımlı bir işi baştan sona yap (Codex/Claude computer use gibi): "
        "her adımda ekranı görür, fare ve klavyeyle tıklar, yazar, kaydırır, uygulama açar, sonucu kontrol edip "
        "düzeltir. Her uygulamada çalışır (UIA vermeyenler dahil: Blender arayüzü, oyunlar, Opera). goal'e işi "
        "kullanıcının istediği gibi, net ve eksiksiz yaz (ör. 'Not Defteri'ni aç, alışveriş listesi yaz ve "
        "masaüstüne liste.txt olarak kaydet'). Tek tıklık işler için reflex/ui_act/browser daha hızlıdır."
    )
    parameters = obj({"goal": {"type": "string"},
                      "monitor": {"type": "integer", "description": "1, 2...; boş = öndeki pencerenin monitörü"},
                      "max_steps": {"type": "integer", "description": "varsayılan 30"}}, ["goal"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, goal: str, max_steps: int = 30, monitor: int = 0) -> ToolResult:
        from alice.computer_agent import ComputerAgent, Screen, router_asker

        if ctx.router is None:
            raise ToolError("model yönlendirici yok")
        agent = ComputerAgent(Screen(ctx, monitor=int(monitor or 0)), router_asker(ctx.router, str(ctx.config.agent.get("computer_role", "vision"))),
                              emit=ctx.emit, stop=ctx.cancelled)
        res = agent.run(goal, max_steps=max(1, min(int(max_steps or 30), 60)))
        return ToolResult(res.report(), is_error=res.status in ("error",))


TOOLS = [Computer(), ComputerTask()]
