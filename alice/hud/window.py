"""HUD penceresi: tkinter ile saydam, çerçevesiz, her zaman üstte; Win32 ayarlarıyla tıklama geçer, odak almaz.

Oyunun FPS'ine dokunmamak için pencere yalnızca çizilecek bir şey varken görünür; boşken gizlenir (ekran
birleştiricisi tam ekran saydam bir katmanla uğraşmaz). Gösterme her zaman SW_SHOWNOACTIVATE ile: oyundayken
öne gelip odak çalan pencere kullanıcıyı öldürmüştü, bu sınır kesindir.
"""

from __future__ import annotations

import argparse
import math
import queue
import socket
import sys
import threading
import time
from typing import Any, Callable

from alice.hud import PORT
from alice.hud.model import HudModel, Item, clock_of, parse

IS_WIN = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HUD_EXSTYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40

KEY = "#010101"  # saydam renk: kenar yumuşatması buna karışınca koyu bir çerçeve olur, göze batmaz
COLORS = {"info": "#40c4ff", "warn": "#ffb000", "critical": "#ff3b30"}
TICK_MS = 30            # komut kuyruğu bu sıklıkla boşaltılır (0,3 sn hedefinin çok altında)
TOPMOST_EVERY = 2.0     # kenarlıksız oyun öne geçerse en üstte olmayı yeniden iste (odak almadan)
EXIT_BUSY = 3           # port başka bir HUD'da: çıkış kodu (denetleyici bunu çökme saymaz)


# ------------------------------------------------------------------------- ağ
def bind(port: int = PORT) -> socket.socket | None:
    """127.0.0.1'de UDP dinle. Port doluysa (başka HUD açık) None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


class Receiver:
    """Soketten gelen datagramları ayrıştırıp kuyruğa koyar; çizim tkinter iş parçacığında kalır."""

    def __init__(self, sock: socket.socket, out: "queue.Queue[dict[str, Any]]"):
        self.sock, self.out = sock, out
        self.sock.settimeout(0.5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="alice-hud-recv")

    def start(self) -> "Receiver":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue  # Windows: ConnectionResetError gibi geçici hatalar dinlemeyi bitirmesin
            msg = parse(raw)
            if msg is not None:
                self.out.put(msg)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------- Win32
def _dpi_aware() -> None:
    from alice.tools.computer import _dpi_aware as aware

    aware()


def monitor_rect(index: int = 0) -> tuple[int, int, int, int] | None:
    """index. monitörün (x, y, genişlik, yükseklik) fiziksel piksel cinsinden; 0 ana monitör."""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    rects: list[tuple[bool, tuple[int, int, int, int]]] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)

    def cb(h, _dc, _r, _l):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(h, ctypes.byref(mi)):
            r = mi.rcMonitor
            rects.append((bool(mi.dwFlags & 1), (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    rects.sort(key=lambda p: (not p[0], p[1][0], p[1][1]))  # önce ana monitör, sonra soldan sağa
    if not rects:
        return None
    return rects[index][1] if 0 <= index < len(rects) else rects[0][1]


class Win32:
    """Pencereye uygulanan Win32 çağrıları; testte sahtesiyle değiştirilir."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.user32 = u = ctypes.windll.user32
        get = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        put = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        get.argtypes, get.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
        put.argtypes, put.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        self._get, self._put = get, put

    def style(self, hwnd: int) -> None:
        self._put(hwnd, GWL_EXSTYLE, self._get(hwnd, GWL_EXSTYLE) | HUD_EXSTYLE)

    def exclude_from_capture(self, hwnd: int) -> bool:
        # Windows 10 2004 öncesinde yok: o zaman ALICE kendi okunu ekran görüntüsünde görebilir, çizim yine çalışır.
        return bool(self.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))

    def show(self, hwnd: int, visible: bool) -> None:
        self.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if visible else SW_HIDE)
        if visible:
            self.topmost(hwnd)

    def topmost(self, hwnd: int) -> None:
        self.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# ------------------------------------------------------------------------ çizim
def direction(clock: int) -> tuple[float, float]:
    """Saat yönünün ekrandaki birim vektörü (12 yukarı, 3 sağ)."""
    a = math.radians(clock * 30)
    return math.sin(a), -math.cos(a)


def edge_point(clock: int, w: int, h: int) -> tuple[float, float, bool]:
    """Merkezden saat yönüne giden ışının ekran kenarına değdiği nokta; True ise sol/sağ kenar."""
    dx, dy = direction(clock)
    tx = (w / 2) / abs(dx) if abs(dx) > 1e-6 else math.inf
    ty = (h / 2) / abs(dy) if abs(dy) > 1e-6 else math.inf
    t = min(tx, ty)
    return w / 2 + dx * t, h / 2 + dy * t, tx <= ty


class Painter:
    """Model öğelerini bir tkinter Canvas'a çizer."""

    def __init__(self, canvas, w: int, h: int, origin: tuple[int, int] = (0, 0)):
        self.c, self.w, self.h, self.origin = canvas, w, h, origin
        self.font = "Segoe UI"

    def text(self, x: float, y: float, s: str, color: str, size: int = 14, anchor: str = "center",
             bold: bool = True) -> None:
        font = (self.font, size, "bold" if bold else "normal")
        for ox, oy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, 2)):  # siyah gölge: her arka planda okunsun
            self.c.create_text(x + ox, y + oy, text=s, fill="#000000", font=font, anchor=anchor)
        self.c.create_text(x, y, text=s, fill=color, font=font, anchor=anchor)

    def draw(self, items: list[Item], now: float) -> None:
        self.c.delete("all")
        cards = progress = 0
        for it in items:
            color = str(it.data.get("color") or COLORS[it.data["severity"]])
            pulse = 0.5 + 0.5 * math.sin((now - it.born) * 8)
            if it.kind == "arrow":
                self.arrow(clock_of(it.data["clock"]) or 12, str(it.data.get("text") or ""), color)
            elif it.kind == "edge":
                self.edge(clock_of(it.data["clock"]) or 12, color, pulse)
            elif it.kind == "label":
                x = it.data.get("x")
                y = it.data.get("y")
                lx = self.w / 2 if x is None else float(x) - self.origin[0]
                ly = self.h * 0.12 if y is None else float(y) - self.origin[1]
                self.text(lx, ly, str(it.data["text"]), color, int(it.data.get("size") or 18))
            elif it.kind == "card":
                self.card(cards, str(it.data.get("title") or ""), str(it.data.get("text") or ""), color)
                cards += 1
            elif it.kind == "box":
                d = it.data
                x, y = float(d["x"]) - self.origin[0], float(d["y"]) - self.origin[1]
                self.c.create_rectangle(x, y, x + float(d["w"]), y + float(d["h"]), outline="#000000", width=5)
                self.c.create_rectangle(x, y, x + float(d["w"]), y + float(d["h"]), outline=color, width=3)
                if d.get("text"):
                    self.text(x, y - 6, str(d["text"]), color, 12, anchor="sw")
            elif it.kind == "progress":
                self.progress(progress, float(it.data["value"]), str(it.data.get("text") or ""), color)
                progress += 1

    def arrow(self, clock: int, label: str, color: str) -> None:
        dx, dy = direction(clock)
        px, py = -dy, dx
        cx, cy = self.w / 2, self.h / 2
        r = min(self.w, self.h) * 0.22
        length, half = 46, 24
        bx, by = cx + dx * r, cy + dy * r
        tx, ty = cx + dx * (r + length), cy + dy * (r + length)
        pts = [tx, ty, bx + px * half, by + py * half, bx + dx * 12, by + dy * 12, bx - px * half, by - py * half]
        self.c.create_polygon(pts, fill=color, outline="#000000", width=3)
        if label:
            lx, ly = cx + dx * (r + length + 34), cy + dy * (r + length + 34)
            self.text(lx, ly, label, color, 15)

    def edge(self, clock: int, color: str, pulse: float) -> None:
        x, y, side = edge_point(clock, self.w, self.h)
        half = min(self.w, self.h) * 0.14
        for width, shade in ((int(26 + 10 * pulse), "#000000"), (int(14 + 8 * pulse), color), (4, "#ffffff")):
            if side:
                x0 = 0 if x < self.w / 2 else self.w
                self.c.create_line(x0, max(0, y - half), x0, min(self.h, y + half), fill=shade, width=width)
            else:
                y0 = 0 if y < self.h / 2 else self.h
                self.c.create_line(max(0, x - half), y0, min(self.w, x + half), y0, fill=shade, width=width)

    def card(self, slot: int, title: str, body: str, color: str) -> None:
        w, pad = 360, 14
        x1, y0 = self.w - 24, 24 + slot * 104
        x0, y1 = x1 - w, y0 + 92
        self.c.create_rectangle(x0, y0, x1, y1, fill="#12161c", outline=color, width=2)
        self.c.create_rectangle(x0, y0, x0 + 5, y1, fill=color, outline=color)
        self.c.create_text(x0 + pad + 4, y0 + pad, text=title, fill=color, font=(self.font, 13, "bold"), anchor="nw")
        self.c.create_text(x0 + pad + 4, y0 + pad + 26, text=body, fill="#e8eef4", font=(self.font, 11),
                           anchor="nw", width=w - 2 * pad - 4)

    def progress(self, slot: int, value: float, label: str, color: str) -> None:
        value = min(1.0, max(0.0, value))
        w, h = 380, 12
        x0, y0 = (self.w - w) / 2, self.h - 90 - slot * 44
        if label:
            self.text(x0, y0 - 6, label, "#e8eef4", 11, anchor="sw", bold=False)
        self.c.create_rectangle(x0, y0, x0 + w, y0 + h, fill="#12161c", outline="#000000", width=2)
        if value > 0:
            self.c.create_rectangle(x0, y0, x0 + w * value, y0 + h, fill=color, outline="")


# ---------------------------------------------------------------------- pencere
class HudWindow:
    def __init__(self, model: HudModel, inbox: "queue.Queue[dict[str, Any]]", monitor: int = 0,
                 opacity: float = 0.92, win32: Win32 | None = None, alive: Callable[[], bool] = lambda: True):
        import tkinter as tk

        self.model, self.inbox, self.alive = model, inbox, alive
        rect = monitor_rect(monitor)
        self.root = root = tk.Tk()
        root.withdraw()  # stil ayarlanmadan ekrana çıkmasın
        if rect is None:
            rect = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        x, y, w, h = rect
        root.overrideredirect(True)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY, "-topmost", True, "-alpha", min(1.0, max(0.2, opacity)))
        self.canvas = tk.Canvas(root, width=w, height=h, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.painter = Painter(self.canvas, w, h, (x, y))
        self.win32 = win32 if win32 is not None else (Win32() if IS_WIN else None)
        root.update_idletasks()
        self.hwnd = int(root.wm_frame(), 16) if IS_WIN else 0
        self.captured_hidden = False
        if self.win32 is not None and self.hwnd:
            self.win32.style(self.hwnd)
            self.captured_hidden = self.win32.exclude_from_capture(self.hwnd)
        self.shown = False
        self._drawn = -1
        self._topmost_at = 0.0
        self._quit = False

    def _set_shown(self, visible: bool) -> None:
        if visible == self.shown:
            return
        self.shown = visible
        if self.win32 is not None and self.hwnd:
            self.win32.show(self.hwnd, visible)
        elif visible:  # Windows dışı: yalnızca deneme için
            self.root.deiconify()
        else:
            self.root.withdraw()

    def tick(self) -> None:
        now = time.monotonic()
        while True:
            try:
                self.model.apply(self.inbox.get_nowait(), now)
            except queue.Empty:
                break
        self.model.expire(now)
        items = self.model.visible()
        animated = any(i.kind == "edge" for i in items)
        if items and (self.model.version != self._drawn or animated):
            self.painter.draw(items, now)
            self._drawn = self.model.version
        self._set_shown(bool(items))
        if not items and self._drawn != self.model.version:
            self.canvas.delete("all")
            self._drawn = self.model.version
        if self.shown and self.win32 is not None and now - self._topmost_at > TOPMOST_EVERY:
            self._topmost_at = now
            self.win32.topmost(self.hwnd)

    def _loop(self) -> None:
        if self._quit or not self.alive():
            self.root.destroy()
            return
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - tek bozuk öğe HUD'u düşürmesin
            pass
        self.root.after(TICK_MS, self._loop)

    def run(self) -> None:
        self.root.after(TICK_MS, self._loop)
        self.root.mainloop()

    def quit(self) -> None:
        self._quit = True


# ---------------------------------------------------------------------- komut
DEMO = [
    {"kind": "arrow", "clock": 4, "text": "SAM · 12 km", "severity": "warn", "id": "demo-sam"},
    {"kind": "edge", "clock": 7, "severity": "critical", "id": "demo-six"},
    {"kind": "arrow", "clock": 7, "text": "ARKANDA · 1,2 km", "severity": "critical", "id": "demo-six-arrow"},
    {"kind": "card", "title": "ALICE HUD", "text": "Deneme: tıklamalar alttaki pencereye geçer, odak kaymaz. "
     "Gizle/göster: kısayol."},
    {"kind": "progress", "value": 0.62, "text": "Yakıt %62"},
]


def _parent_alive(pid: int) -> Callable[[], bool]:
    """Çekirdek kapanınca (os._exit ile bile) HUD sahipsiz kalmasın."""
    if not pid:
        return lambda: True
    import psutil

    state = {"t": 0.0, "ok": True}

    def alive() -> bool:
        now = time.monotonic()
        if now - state["t"] > 2.0:
            state["t"], state["ok"] = now, psutil.pid_exists(pid)
        return state["ok"]

    return alive


def hud_cmd(argv: list[str]) -> int:
    from alice.cli import TrArgumentParser
    from alice.config import load_config

    ap = TrArgumentParser(prog="alice hud", description="Ekran üstü HUD: tıklama geçen, odak almayan saydam katman.")
    ap.add_argument("--demo", action="store_true", help="ok, kenar parlaması, kart ve ilerleme çubuğu çiz (30 sn)")
    ap.add_argument("--port", type=int, default=None, help=f"komut portu (varsayılan [hud] port ya da {PORT})")
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)  # çekirdeğin pid'i
    a = ap.parse_args(argv)
    hud = load_config().data.get("hud", {}) or {}
    port = int(a.port or hud.get("port") or PORT)
    if a.demo:
        return _demo(port, hud)
    return run(port, hud, alive=_parent_alive(a.parent))


def run(port: int, hud: dict[str, Any], alive: Callable[[], bool] = lambda: True,
        seed: list[dict[str, Any]] | None = None, seconds: float = 0.0) -> int:
    _dpi_aware()  # koordinatlar fiziksel piksel: %125/%150 ölçekte de kutular doğru yere düşsün
    sock = bind(port)
    if sock is None:
        return EXIT_BUSY
    inbox: queue.Queue[dict[str, Any]] = queue.Queue()
    for m in seed or []:
        inbox.put(m)
    rx = Receiver(sock, inbox).start()
    end = time.monotonic() + seconds if seconds else 0.0
    win = HudWindow(HudModel(), inbox, monitor=int(hud.get("monitor", 0) or 0),
                    opacity=float(hud.get("opacity", 0.92) or 0.92),
                    alive=lambda: alive() and (not end or time.monotonic() < end))
    keys = None
    combo = str(hud.get("hotkey", "<ctrl>+<alt>+h") or "")
    if IS_WIN and combo:
        from alice.hotkeys import WinHotkeys

        keys = WinHotkeys({combo: lambda: inbox.put({"kind": "toggle"})})
        keys.start()
    try:
        win.run()
    finally:
        rx.stop()
        if keys is not None:
            keys.stop()
    return 0


def _demo(port: int, hud: dict[str, Any]) -> int:
    seed = [{**m, "ttl": 30} for m in DEMO]
    code = run(port, hud, seed=seed, seconds=31)
    if code == EXIT_BUSY:  # HUD zaten açık (çekirdek başlatmış): denemeyi ona gönder
        from alice.hud import client

        client.configure(port)
        for m in seed:
            client.send(m)
        print("HUD zaten açık; deneme ona gönderildi.")
        return 0
    return code
