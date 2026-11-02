"""Eylem sonrası gözlem: ekranı etkileyen bir araçtan sonra gerçekte ne oldu?

ALICE eyleminin sonucunu varsayıyordu: "başlatma komutu gönderildi"yi "açıldı" sandı, ekrana çıkan Windows hata
kutusunu ("... bulunamadı") ya da Farklı Kaydet penceresini fark etmeden körlemesine Enter'a bastı. Bu her uygulamada
olabilir; bu yüzden araca özel değil, genel: eylemden önce ve sonra üst düzey pencereler karşılaştırılır, yeni açılan
pencereler (özellikle iletişim kutuları, içlerindeki yazıyla) ve Windows izin ekranı (UAC) araç sonucuna eklenir.
"Aç" denip hiçbir pencere görünmediyse bu da söylenir. Model kararını varsayıma değil, ekranda olana göre verir.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

IS_WIN = sys.platform == "win32"
DIALOG_CLASS = "#32770"  # Windows iletişim kutusu (hata/uyarı kutuları, Farklı Kaydet, Aç...)
SETTLE = (0.2, 0.25)  # sn: yeni pencere için en fazla ~0,45 sn bakılır, çıkınca beklemez (geç açılan kutu bir
                      # sonraki eylemde "önde açık bir iletişim kutusu var" diye yakalanır)
OPEN_WAIT = 1.6  # sn: "aç" eyleminde pencere çıkmadıysa bu kadar beklenip söylenir
TEXT_MAX = 300
# Bunlar pencere değil (araç çubuğu ipuçları, gölgeler, Windows'un kendi katmanları)
IGNORE_CLASSES = {"tooltips_class32", "SysShadow", "Shell_TrayWnd", "Progman", "WorkerW", "IME", "MSCTFIME UI",
                  "ForegroundStaging", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"}


def _win32():
    import ctypes
    from ctypes import wintypes

    return ctypes, wintypes, ctypes.windll.user32


def _text(user32, ctypes, hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(user32, ctypes, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def windows() -> dict[int, dict[str, Any]]:
    """Görünür üst düzey pencereler: hwnd -> {title, cls, pid}. ALICE'in kendi pencereleri (HUD) hariç."""
    if not IS_WIN:
        return {}
    ctypes, wintypes, user32 = _win32()
    out: dict[int, dict[str, Any]] = {}
    me = os.getpid()
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        cls = _class(user32, ctypes, h)
        title = _text(user32, ctypes, h)
        if cls in IGNORE_CLASSES or (not title and cls != DIALOG_CLASS) or (cls == "TkTopLevel" and title == "tk"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == me:
            return True
        out[int(h)] = {"title": title, "cls": cls, "pid": int(pid.value)}
        return True

    user32.EnumWindows(CB(cb), 0)
    return out


def dialog_text(hwnd: int) -> str:
    """İletişim kutusunun içindeki yazılar (mesaj ve düğmeler)."""
    if not IS_WIN:
        return ""
    ctypes, wintypes, user32 = _win32()
    parts: list[str] = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if user32.IsWindowVisible(h) and _class(user32, ctypes, h) in ("Static", "Button"):
            t = " ".join(_text(user32, ctypes, h).replace("&", "").split())
            if t and t not in parts:
                parts.append(t)
        return sum(map(len, parts)) < TEXT_MAX

    user32.EnumChildWindows(hwnd, CB(cb), 0)
    return " | ".join(parts)[:TEXT_MAX]


def foreground() -> int:
    if not IS_WIN:
        return 0
    return int(_win32()[2].GetForegroundWindow() or 0)


def uac_open() -> bool:
    """Windows izin ekranı (Kullanıcı Hesabı Denetimi) açık mı? Güvenli masaüstünde: hiçbir program tıklayamaz."""
    try:
        import psutil

        return any((p.info.get("name") or "").lower() == "consent.exe" for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001
        return False


class Watch:
    """Eylemden önce `Watch()`, sonra `note()`: yeni pencereler, iletişim kutuları, UAC."""

    def __init__(self, opening: bool = False):
        self.opening = opening
        try:
            self.before = windows()
            self.uac = uac_open()
        except Exception:  # noqa: BLE001 - gözlem bozuksa eylem yine sürer, sadece not olmaz
            self.before, self.uac = {}, False

    def note(self, sleep=time.sleep) -> str:
        if not IS_WIN:
            return ""
        try:
            return self._note(sleep)
        except Exception:  # noqa: BLE001
            return ""

    def _note(self, sleep) -> str:
        new: dict[int, dict[str, Any]] = {}
        waited = 0.0
        for step in SETTLE + ((OPEN_WAIT - sum(SETTLE),) if self.opening else ()):
            sleep(step)
            waited += step
            after = windows()
            new = {h: w for h, w in after.items() if h not in self.before}
            if new:
                break
        lines: list[str] = []
        fg = foreground()
        for h, w in list(new.items())[:4]:
            if w["cls"] == DIALOG_CLASS:
                body = dialog_text(h)
                lines.append(f"yeni iletişim kutusu '{w['title']}'" + (f": {body}" if body else ""))
            else:
                lines.append(f"yeni pencere '{w['title']}'")
        if fg and fg not in new and fg in self.before and self.before[fg]["cls"] == DIALOG_CLASS:
            body = dialog_text(fg)
            lines.append(f"önde açık bir iletişim kutusu var '{self.before[fg]['title']}'" + (f": {body}" if body else ""))
        if not self.uac and uac_open():
            lines.append("Windows izin ekranı (UAC) açıldı: hiçbir program ona tıklayamaz; kullanıcıdan 'Evet'e "
                         "basmasını iste ve bekle")
        if self.opening and not new and not lines:
            lines.append(f"{waited:.1f} sn içinde yeni bir pencere görünmedi: açıldığını varsayma, kontrol et "
                         "(ya hiç açılmadı ya da yavaş açılıyor)")
        if not lines:
            return ""
        dialog = any("iletişim kutusu" in s for s in lines)
        tail = (" Beklemediğin bir uyarı/hata ise önce ne yazdığını oku ve planını ona göre değiştir; körlemesine "
                "Enter/Tamam'a basma, aynı adımı tekrarlama.") if dialog else ""
        return "\n\n[ekranda olan] " + "; ".join(lines) + "." + tail
