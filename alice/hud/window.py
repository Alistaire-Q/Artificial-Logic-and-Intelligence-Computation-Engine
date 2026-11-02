"""HUD penceresi: her öğe kendi küçük, çerçevesiz, her zaman üstte penceresinde; tıklama geçer, odak almaz.

Görünüm alice/hud/style.py'de (kenarı yumuşatılmış, yarı saydam resimler). Resim pencereye UpdateLayeredWindow
ile piksel başı saydamlıkla basılır: tkinter'ın "saydam renk" yöntemi yarı saydamlık, yumuşak gölge ve kenar
yumuşatma vermiyordu, her şey keskin ve siyah çerçeveli görünüyordu. Ekranı kaplayan tek pencere yerine öğe başına
küçük pencere: animasyonda sadece o küçük resim yenilenir, oyunun FPS'ine dokunmaz. Öğe yoksa hiç pencere yok.

Gösterme her zaman SW_SHOWNOACTIVATE ile: oyundayken öne gelip odak çalan pencere kullanıcıyı öldürmüştü, bu sınır
kesindir. tkinter sadece olay döngüsü ve pencere tutucu olarak kullanılır.
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

from alice.hud import PORT, style
from alice.hud.model import HudModel, parse

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
ULW_ALPHA = 0x2

TICK_MS = 16            # animasyon karesi ve komut kuyruğu; sadece hareket eden öğe yeniden basılır
TOPMOST_EVERY = 2.0     # kenarlıksız oyun öne geçerse en üstte olmayı yeniden iste (odak almadan)
EXIT_BUSY = 3           # port başka bir HUD'da: çıkış kodu (denetleyici bunu çökme saymaz)
ISLAND_KEY = "__ada__"  # Adanın penceresi


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
                self._stop.wait(0.2)  # geçici hata dinlemeyi bitirmesin, kalıcı hata da çekirdeği yemesin
                continue
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

        self.ctypes, self.wt = ctypes, wintypes
        self.user32 = u = ctypes.windll.user32
        self.gdi32 = g = ctypes.windll.gdi32
        get = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        put = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        get.argtypes, get.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t
        put.argtypes, put.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        self._get, self._put = get, put

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [("op", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("alpha", ctypes.c_ubyte),
                        ("fmt", ctypes.c_ubyte)]

        self._bmi, self._blend = BITMAPINFOHEADER, BLENDFUNCTION
        g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = wintypes.HDC, [wintypes.HDC]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
        g.SelectObject.restype, g.SelectObject.argtypes = wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        u.GetDC.restype, u.GetDC.argtypes = wintypes.HDC, [wintypes.HWND]
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
            ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]

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

    def update_layered(self, hwnd: int, img, x: int, y: int) -> bool:
        """RGBA resmi pencereye piksel başı saydamlıkla bas ve pencereyi (x, y)'ye taşı."""
        ct, wt, g, u = self.ctypes, self.wt, self.gdi32, self.user32
        w, h = img.size
        data = img.convert("RGBa").tobytes("raw", "BGRa")  # önceden çarpılmış alfa: ULW bunu ister
        bmi = self._bmi(ct.sizeof(self._bmi), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        screen = u.GetDC(None)
        mem = g.CreateCompatibleDC(screen)
        bits = ct.c_void_p()
        bmp = g.CreateDIBSection(mem, ct.byref(bmi), 0, ct.byref(bits), None, 0)
        ok = False
        if bmp and bits.value:
            ct.memmove(bits, data, len(data))
            old = g.SelectObject(mem, bmp)
            ok = bool(u.UpdateLayeredWindow(hwnd, screen, ct.byref(wt.POINT(x, y)), ct.byref(wt.SIZE(w, h)), mem,
                                            ct.byref(wt.POINT(0, 0)), 0, ct.byref(self._blend(0, 0, 255, 1)),
                                            ULW_ALPHA))
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)
        return ok


# ------------------------------------------------------------------------ yön
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


# ---------------------------------------------------------------------- pencere
class Slot:
    """Ekrandaki bir öğenin penceresi ve animasyonu."""

    def __init__(self, top: Any, hwnd: int, anim: style.Anim):
        self.top, self.hwnd, self.anim = top, hwnd, anim
        self.sprite: style.Sprite | None = None
        self.base: tuple[Any, Any] | None = None  # (anahtar, resim): değişmeyen öğe yeniden çizilmez
        self.visible = False
        self.last: tuple | None = None


def _tk_slot(root: Any) -> tuple[Any, int]:
    """Gizli bir tkinter penceresi aç: tutucusu (hwnd) katmanlı pencere olarak kullanılır."""
    import tkinter as tk

    top = tk.Toplevel(root)
    top.withdraw()
    top.overrideredirect(True)
    top.geometry("1x1+-32000+-32000")
    top.update_idletasks()
    return top, (int(top.wm_frame(), 16) if IS_WIN else 0)


class HudWindow:
    def __init__(self, model: HudModel, inbox: "queue.Queue[dict[str, Any]]", monitor: int = 0,
                 opacity: float = 0.95, win32: Win32 | None = None, alive: Callable[[], bool] = lambda: True,
                 root: Any = None, rect: tuple[int, int, int, int] | None = None,
                 make_slot: Callable[[Any], tuple[Any, int]] = _tk_slot):
        self.model, self.inbox, self.alive = model, inbox, alive
        self.opacity = min(1.0, max(0.2, opacity))
        if root is None:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()  # olay döngüsü için; kendisi hiç görünmez
        self.root = root
        if rect is None:
            rect = monitor_rect(monitor) or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        self.ox, self.oy, self.w, self.h = rect
        self.win32 = win32 if win32 is not None else (Win32() if IS_WIN else None)
        self.make_slot = make_slot
        self.island = style.Island(self.w, self.h)
        self.slots: dict[str, Slot] = {}
        self.shown = False
        self._topmost_at = 0.0
        self._quit = False

    def _new_slot(self, now: float) -> Slot:
        top, hwnd = self.make_slot(self.root)
        if self.win32 is not None and hwnd:
            self.win32.style(hwnd)
            self.win32.exclude_from_capture(hwnd)
        return Slot(top, hwnd, style.Anim(now))

    def _drop(self, key: str) -> None:
        slot = self.slots.pop(key)
        if self.win32 is not None and slot.hwnd and slot.visible:
            self.win32.show(slot.hwnd, False)
        try:
            slot.top.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _paint(self, slot: Slot, now: float) -> bool:
        """Bu karede pencereyi güncelle; öğe hâlâ hareket ediyorsa True."""
        sp = slot.sprite
        alpha, grow, pulse, moving = slot.anim.frame(now, sp)
        pulse = round(pulse * 8) / 8  # nefes 9 basamak: önbellek tutsun
        key = (sp.item.seq, sp.w, sp.h, round(sp.angle), pulse)
        if slot.base is None or slot.base[0] != key:
            slot.base = (key, style.render(sp, self.h, pulse=pulse))
        a = alpha * self.opacity
        frame = (round(a, 2), round(grow, 3), key, sp.x, sp.y)
        if frame == slot.last:
            return moving
        slot.last = frame
        img = style.adjust(slot.base[1], a, grow)
        if self.win32 is not None and slot.hwnd:
            self.win32.update_layered(slot.hwnd, img, self.ox + sp.x, self.oy + sp.y)
            if not slot.visible:
                self.win32.show(slot.hwnd, True)
        slot.visible = True
        return moving

    def _paint_island(self, now: float) -> None:
        """Ada: tek pencere, içerik ve boyut style.Island'da. Hareket yokken yeniden basılmaz."""
        visible = self.island.update(style.entries(self.model.visible()), now)
        slot = self.slots.get(ISLAND_KEY)
        if not visible:
            if slot is not None:
                self._drop(ISLAND_KEY)
            return
        if slot is None:
            slot = self.slots[ISLAND_KEY] = self._new_slot(now)
        sig = (self.island.current and style._sig(self.island.current),
               tuple(style._sig(e) for e in self.island.minimals))
        img, moving = self.island.frame(now)
        if not moving and slot.last == sig:
            return
        slot.last = sig if not moving else None
        if self.opacity < 0.999:
            img = style.adjust(img, self.opacity)
        if self.win32 is not None and slot.hwnd:
            self.win32.update_layered(slot.hwnd, img, self.ox + self.island.x, self.oy + self.island.y)
            if not slot.visible:
                self.win32.show(slot.hwnd, True)
        slot.visible = True

    def tick(self) -> None:
        now = time.monotonic()
        while True:
            try:
                self.model.apply(self.inbox.get_nowait(), now)
            except queue.Empty:
                break
        self.model.expire(now)
        sprites = {sp.key: sp for sp in style.layout(self.model.visible(), self.w, self.h)}
        for key, sp in sprites.items():
            slot = self.slots.get(key)
            if slot is not None and slot.anim.gone_at is not None:  # kaybolurken geri geldi: baştan
                self._drop(key)
                slot = None
            if slot is None:
                slot = self.slots[key] = self._new_slot(now)
            slot.sprite = sp
        for key, slot in self.slots.items():
            if key != ISLAND_KEY and key not in sprites and slot.anim.gone_at is None:
                slot.anim.gone_at = now  # kaybolurken solsun
        for key, slot in list(self.slots.items()):
            if key == ISLAND_KEY:
                continue
            moving = self._paint(slot, now)
            if slot.anim.gone_at is not None and not moving:
                self._drop(key)
        self._paint_island(now)
        self.shown = bool(self.slots)
        if self.shown and self.win32 is not None and now - self._topmost_at > TOPMOST_EVERY:
            self._topmost_at = now
            for slot in self.slots.values():
                if slot.hwnd and slot.visible:
                    self.win32.topmost(slot.hwnd)

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
    {"kind": "arrow", "clock": 2, "text": "SAM · 12 km", "severity": "warn", "id": "demo-sam"},
    {"kind": "edge", "clock": 7, "severity": "critical", "id": "demo-six.edge"},
    {"kind": "arrow", "clock": 7, "text": "Arkanda · 1,2 km", "severity": "critical", "id": "demo-six"},
    {"kind": "label", "text": "Yakıt ~6 dk", "severity": "warn", "id": "demo-fuel"},
    {"kind": "card", "title": "ALICE", "text": "Deneme: tıklamalar alttaki pencereye geçer, odak kaymaz. "
     "Gizle/göster: Ctrl+Alt+H."},
    {"kind": "progress", "value": 0.62, "text": "GPU 84°C", "severity": "info", "id": "demo-gpu"},
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
    ap.add_argument("--demo", action="store_true", help="ok, kenar ışığı, kart ve gösterge çiz (30 sn)")
    ap.add_argument("--preview", metavar="DOSYA", help="denemeyi pencere açmadan PNG olarak kaydet")
    ap.add_argument("--port", type=int, default=None, help=f"komut portu (varsayılan [hud] port ya da {PORT})")
    ap.add_argument("--parent", type=int, default=0, help=argparse.SUPPRESS)  # çekirdeğin pid'i
    a = ap.parse_args(argv)
    hud = load_config().data.get("hud", {}) or {}
    port = int(a.port or hud.get("port") or PORT)
    if a.preview:
        style.preview(DEMO).save(a.preview)
        print(f"Kaydedildi: {a.preview}")
        return 0
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
                    opacity=float(hud.get("opacity", 0.95) or 0.95),
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
