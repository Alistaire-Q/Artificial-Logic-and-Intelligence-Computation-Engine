"""Masaüstü: pencereler, uygulama açma, pano ve Windows UI Automation ağacı.

ui_elements, ekrandaki pencerenin tıklanabilir öğelerini ID'li bir listeye çevirir.
Bu liste hem beyin modeline (pixel tahmin etmeden tıklasın diye) hem de Jev reflex
döngüsüne aday menüsü olarak verilir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

INTERACTIVE = {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "ListItem", "TabItem",
               "Hyperlink", "TreeItem", "SplitButton", "Slider", "Spinner", "DataItem", "MenuBar", "Document"}


# ------------------------------------------------------------------ pencereler
def list_windows() -> list[str]:
    if IS_WIN or IS_MAC:
        try:
            import pygetwindow as gw
        except ImportError as e:
            raise ToolError("pygetwindow kurulu değil: pip install 'alice-harness[computer]'") from e
        return [t for t in gw.getAllTitles() if t.strip()]
    if shutil.which("wmctrl"):
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        return [line.split(None, 3)[-1] for line in out.splitlines() if len(line.split(None, 3)) == 4]
    raise ToolError("Linux'ta pencere listesi için wmctrl gerekli (sudo apt install wmctrl)")


def focus_window(title: str) -> str:
    if IS_WIN or IS_MAC:
        import pygetwindow as gw

        wins = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower()]
        if not wins:
            raise ToolError(f"'{title}' başlıklı pencere yok")
        w = wins[0]
        try:
            if getattr(w, "isMinimized", False):
                w.restore()
            w.activate()
        except Exception:  # noqa: BLE001 - Windows bazen ön plana almayı reddeder; alt hilesi
            if IS_WIN:
                import pyautogui

                pyautogui.press("alt")
                w.activate()
        return w.title
    subprocess.run(["wmctrl", "-a", title], check=False)
    return title


def window_op(title: str, op: str) -> str:
    if not (IS_WIN or IS_MAC):
        flags = {"close": ["-c"], "maximize": ["-r", title, "-b", "add,maximized_vert,maximized_horz"]}
        if op == "close":
            subprocess.run(["wmctrl", "-c", title], check=False)
        elif op == "maximize":
            subprocess.run(["wmctrl", *flags["maximize"]], check=False)
        else:
            raise ToolError(f"Linux'ta {op} desteklenmiyor")
        return title
    import pygetwindow as gw

    wins = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower()]
    if not wins:
        raise ToolError(f"'{title}' başlıklı pencere yok")
    w = wins[0]
    {"minimize": w.minimize, "maximize": w.maximize, "restore": w.restore, "close": w.close}[op]()
    return w.title


def _shortcut_roots() -> list[Path]:
    return [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path.home() / "Desktop", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]


def _start_menu_lookup(name: str) -> Path | None:
    roots = _shortcut_roots()
    needle = name.lower()
    best: Path | None = None
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() not in (".lnk", ".url", ".appref-ms"):
                continue
            stem = p.stem.lower()
            if stem == needle:
                return p
            if needle in stem and (best is None or len(stem) < len(best.stem)):
                best = p
    return best


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _resolve_lnks(lnks: list[Path]) -> dict[Path, str]:
    """Kısayolların hedef exe yollarını tek PowerShell çağrısıyla çöz."""
    lnks = [p for p in lnks if p.suffix.lower() == ".lnk"]
    if not lnks:
        return {}
    paths = ",".join("'" + str(p).replace("'", "''") + "'" for p in lnks)
    ps = (f"$w=New-Object -ComObject WScript.Shell;foreach($p in @({paths}))"
          "{try{Write-Output ($p+'|'+$w.CreateShortcut($p).TargetPath)}catch{}}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                           creationflags=0x08000000)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[Path, str] = {}
    for line in r.stdout.decode("utf-8", errors="replace").splitlines():
        lnk, _, target = line.strip().partition("|")
        if target:
            out[Path(lnk)] = target
    return out


def _steam_libraries() -> list[Path]:
    import re

    roots = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
             Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    libs: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        libs.append(root / "steamapps" / "common")
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
                libs.append(Path(m.group(1).replace("\\\\", "\\")) / "steamapps" / "common")
        except OSError:
            pass
    return [p for p in dict.fromkeys(libs) if p.is_dir()]


def _exes_in(folder: Path, needle: str, depth: int = 3) -> list[Path]:
    """Klasörde adı aranan kelimeyi içeren exe'ler (yoksa klasördeki tüm exe'ler), sığ arama."""
    found: list[Path] = []
    stack = [(folder, 0)]
    while stack and len(found) < 40:
        d, lvl = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_file() and e.suffix.lower() == ".exe":
                found.append(e)
            elif e.is_dir() and lvl < depth:
                stack.append((e, lvl + 1))
    named = [f for f in found if needle in _norm(f.stem)]
    skip = ("unins", "setup", "crash", "update", "helper")
    return [f for f in (named or found) if not any(k in f.stem.lower() for k in skip)]


def find_installed_apps(name: str, limit: int = 15) -> list[str]:
    """Kurulu bir programın yerini bul: Başlat menüsü, kayıt defteri (Uninstall, App Paths),
    Program Files, LOCALAPPDATA\\Programs ve Steam kütüphaneleri. Satır başına 'kaynak: yol'."""
    if not IS_WIN:
        exe = shutil.which(name)
        return [f"PATH: {exe}"] if exe else []
    needle = _norm(name)
    if not needle:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(src: str, path: str) -> None:
        path = path.strip().strip('"')
        if path and path.lower() not in seen and Path(path).exists():
            seen.add(path.lower())
            out.append(f"{src}: {path}")

    w = shutil.which(name)
    if w:
        add("PATH", w)

    # Başlat menüsü / masaüstü kısayolları (hedef exe'leriyle birlikte)
    roots = _shortcut_roots()
    lnks: list[Path] = []
    for root in roots:
        if root.is_dir():
            lnks += [p for p in root.rglob("*") if p.suffix.lower() in (".lnk", ".url", ".appref-ms")
                     and needle in _norm(p.stem)]
    targets = _resolve_lnks(lnks[:20])
    for lnk in lnks[:20]:
        t = targets.get(lnk, "")
        if t.lower().endswith(".exe"):
            add("kısayol hedefi", t)
        add("kısayol", str(lnk))

    # Kayıt defteri
    try:
        import winreg

        hives = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                 (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
        for hive, path in hives:
            try:
                root = winreg.OpenKey(hive, path)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                    disp = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                except OSError:
                    continue
                if needle not in _norm(disp):
                    continue
                for val in ("DisplayIcon", "InstallLocation"):
                    try:
                        v = str(winreg.QueryValueEx(sub, val)[0]).split(",")[0].strip('"')
                    except OSError:
                        continue
                    if v.lower().endswith(".exe"):
                        add(f"kayıt ({disp})", v)
                    elif v and Path(v).is_dir():
                        for exe in _exes_in(Path(v), needle, depth=2)[:3]:
                            add(f"kayıt ({disp})", str(exe))
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                root = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(root)[0]):
                key = winreg.EnumKey(root, i)
                if needle in _norm(Path(key).stem):
                    try:
                        add("App Paths", str(winreg.QueryValue(root, key)))
                    except OSError:
                        pass
    except ImportError:
        pass

    # Program klasörleri ve Steam: adı eşleşen klasörlerin içindeki exe'ler
    bases = [Path(os.environ.get(k, "")) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    bases.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs")
    bases += _steam_libraries()
    for base in dict.fromkeys(bases):
        if not str(base) or not base.is_dir():
            continue
        try:
            subs = list(base.iterdir())
        except OSError:
            continue
        for d in subs:
            if not d.is_dir():
                continue
            # "Blender Foundation\Blender 4.2" gibi bir alt seviyeye de bak
            cands = [d] if needle in _norm(d.name) else []
            if not cands:
                try:
                    cands = [c for c in d.iterdir() if c.is_dir() and needle in _norm(c.name)]
                except OSError:
                    cands = []
            for c in cands:
                for exe in _exes_in(c, needle)[:3]:
                    add("klasör", str(exe))
        if len(out) >= limit:
            break
    return out[:limit]


def open_app(target: str) -> str:
    """Uygulama adı, exe yolu, dosya ya da URL aç."""
    if IS_WIN:
        p = Path(os.path.expandvars(target)).expanduser()
        if p.exists() or "://" in target or shutil.which(target):
            os.startfile(str(p) if p.exists() else (shutil.which(target) or target))  # type: ignore[attr-defined]
            return f"açıldı: {target}"
        lnk = _start_menu_lookup(target)
        if lnk:
            os.startfile(str(lnk))  # type: ignore[attr-defined]
            return f"açıldı: {lnk.stem} ({lnk})"
        for line in find_installed_apps(target):
            path = line.split(": ", 1)[1]
            if path.lower().endswith(".exe"):
                os.startfile(path)  # type: ignore[attr-defined]
                return f"açıldı: {path}"
        # Son çare: shell'in kendi çözümlemesi (App Paths kayıtları dahil).
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=0x08000000)
        return (f"'{target}' kurulu programlarda bulunamadı; başlatma komutu yine de gönderildi. "
                "Pencere açılmadıysa program kurulu değildir ya da adı farklıdır.")
    if IS_MAC:
        if Path(target).expanduser().exists() or "://" in target:
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["open", "-a", target])
        return f"açıldı: {target}"
    exe = shutil.which(target)
    if exe:
        subprocess.Popen([exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("gtk-launch") and not Path(target).exists():
        subprocess.Popen(["gtk-launch", target], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", target], start_new_session=True)
    return f"açıldı: {target}"


# ------------------------------------------------------------------- UIA ağacı
def _foreground_uia(title: str | None):
    try:
        from pywinauto import Desktop
    except ImportError as e:
        raise ToolError("pywinauto kurulu değil: pip install 'alice-harness[computer]'") from e
    desk = Desktop(backend="uia")
    if title:
        wins = [w for w in desk.windows() if title.lower() in (w.window_text() or "").lower()]
        if not wins:
            raise ToolError(f"'{title}' başlıklı pencere yok")
        return wins[0]
    import ctypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return desk.window(handle=hwnd).wrapper_object()


def collect_ui_elements(ctx: ToolContext, title: str | None = None, limit: int = 150) -> tuple[str, list[dict[str, Any]]]:
    """Öndeki (ya da başlığı verilen) pencerenin etkileşimli öğelerini topla."""
    if not IS_WIN:
        raise ToolError("ui_elements şimdilik sadece Windows'ta (UI Automation). Linux/macOS'ta computer aracını kullan.")
    win = _foreground_uia(title)
    try:
        descendants = win.descendants(depth=14)
    except TypeError:
        descendants = win.descendants()
    items: list[dict[str, Any]] = []
    wrappers: dict[int, Any] = {}
    for el in descendants:
        try:
            info = el.element_info
            ctype = info.control_type
            if ctype not in INTERACTIVE or not el.is_visible():
                continue
            r = info.rectangle
            if r.width() <= 1 or r.height() <= 1:
                continue
            name = (info.name or "").strip()
            value = ""
            if ctype in ("Edit", "ComboBox", "Document"):
                try:
                    value = (el.get_value() if hasattr(el, "get_value") else el.window_text()) or ""
                except Exception:  # noqa: BLE001
                    value = ""
            if not name and not value and not info.automation_id:
                continue
            idx = len(items) + 1
            wrappers[idx] = el
            items.append({
                "id": idx, "role": ctype, "name": name[:80], "value": str(value)[:80],
                "auto_id": (info.automation_id or "")[:40],
                "enabled": el.is_enabled(),
                "center": ((r.left + r.right) // 2, (r.top + r.bottom) // 2),
            })
            if len(items) >= limit:
                break
        except Exception:  # noqa: BLE001 - UIA öğeleri tararken kaybolabilir
            continue
    ctx.state["ui_elems"] = wrappers
    ctx.state["ui_items"] = {i["id"]: i for i in items}
    return win.window_text(), items


def format_ui(title: str, items: list[dict[str, Any]]) -> str:
    rows = [f"pencere: {title}"]
    for i in items:
        bits = [f"[{i['id']}] {i['role']}"]
        if i["name"]:
            bits.append(f"'{i['name']}'")
        if i["value"]:
            bits.append(f"değer='{i['value']}'")
        if not i["enabled"]:
            bits.append("(pasif)")
        rows.append(" ".join(bits))
    return "\n".join(rows)


def ui_act(ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> str:
    wrappers = ctx.state.get("ui_elems") or {}
    el = wrappers.get(int(element_id))
    if el is None:
        raise ToolError("öğe bulunamadı; önce ui_elements çağır (liste her çağrıda yenilenir)")
    if action == "click":
        try:
            el.invoke()
        except Exception:  # noqa: BLE001 - invoke pattern'i olmayan öğeler
            el.click_input()
    elif action == "double_click":
        el.double_click_input()
    elif action == "right_click":
        el.right_click_input()
    elif action == "focus":
        el.set_focus()
    elif action == "set_text":
        if text is None:
            raise ToolError("set_text için text gerekli")
        try:
            el.set_edit_text(text)
        except Exception:  # noqa: BLE001
            el.set_focus()
            from alice.tools.computer import press_keys, type_text

            press_keys("ctrl+a")
            type_text(text)
    elif action == "type":
        el.set_focus()
        from alice.tools.computer import type_text

        type_text(text or "")
    elif action in ("toggle", "expand", "collapse", "select"):
        getattr(el, action)()
    else:
        raise ToolError(f"bilinmeyen aksiyon: {action}")
    time.sleep(0.2)
    item = (ctx.state.get("ui_items") or {}).get(int(element_id), {})
    return f"{action} -> [{element_id}] {item.get('role', '')} '{item.get('name', '')}'"


# ----------------------------------------------------------------------- araçlar
class Windows(Tool):
    name = "windows"
    description = ("Pencere yönetimi, kurulu program bulma ve uygulama açma. "
                   "action: list | focus | minimize | maximize | restore | close | open | find. "
                   "open için target: uygulama adı (ör. 'Discord', 'notepad', 'blender'), exe yolu, dosya ya da URL "
                   "(URL kullanıcının varsayılan tarayıcısında, kendi girişleriyle açılır). "
                   "Zaten açık bir pencerede iş için: list ile bul, focus ile öne al, sonra ui_elements/ui_act. "
                   "find: kurulu bir programın exe yolunu bulur (Başlat menüsü, kayıt defteri, Program Files, Steam); "
                   "bir programın kurulu olup olmadığını ya da exe yolunu (ör. komut satırından script çalıştırmak için) "
                   "öğrenmek için shell'de arama yapmak yerine bunu kullan.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "restore", "close", "open", "find"]},
        "title": {"type": "string", "description": "pencere başlığının bir parçası"},
        "target": {"type": "string"},
    }, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") in ("list", "find") else COMPUTER

    def run(self, ctx: ToolContext, action: str, title: str | None = None, target: str | None = None) -> ToolResult:
        if action == "list":
            return ToolResult("\n".join(list_windows()) or "pencere yok")
        if action == "find":
            if not target:
                raise ToolError("find için target gerekli")
            hits = find_installed_apps(target)
            return ToolResult("\n".join(hits) if hits else f"'{target}' kurulu programlarda bulunamadı "
                              "(Başlat menüsü, kayıt defteri, Program Files, Steam tarandı).")
        if action == "open":
            if not target:
                raise ToolError("open için target gerekli")
            msg = open_app(target)
            time.sleep(1.5)
            return ToolResult(msg)
        if not title:
            raise ToolError(f"{action} için title gerekli")
        if action == "focus":
            return ToolResult(f"öne alındı: {focus_window(title)}")
        return ToolResult(f"{action}: {window_op(title, action)}")


class UIElements(Tool):
    name = "ui_elements"
    description = ("Windows: öndeki (ya da title verilen) pencerenin tıklanabilir öğelerini ID'leriyle listele. "
                   "Pixel tahmini yerine bunu kullan, sonra ui_act ile ID üzerinden işlem yap.")
    parameters = obj({"title": {"type": "string"}, "limit": {"type": "integer"}})

    def run(self, ctx: ToolContext, title: str | None = None, limit: int = 150) -> ToolResult:
        t, items = collect_ui_elements(ctx, title, limit)
        return ToolResult(format_ui(t, items))


class UIAct(Tool):
    name = "ui_act"
    description = "ui_elements'ten gelen bir öğe ID'si üzerinde işlem: click, double_click, right_click, focus, set_text, type, toggle, expand, collapse, select."
    parameters = obj({
        "element_id": {"type": "integer"},
        "action": {"type": "string", "enum": ["click", "double_click", "right_click", "focus", "set_text", "type",
                                              "toggle", "expand", "collapse", "select"]},
        "text": {"type": "string"},
    }, ["element_id", "action"])
    risk = COMPUTER

    def run(self, ctx: ToolContext, element_id: int, action: str, text: str | None = None) -> ToolResult:
        return ToolResult(ui_act(ctx, element_id, action, text))


class Clipboard(Tool):
    name = "clipboard"
    description = "Panoyu oku (action=get) ya da yaz (action=set)."
    parameters = obj({"action": {"type": "string", "enum": ["get", "set"]}, "text": {"type": "string"}}, ["action"])
    risk = COMPUTER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "get" else COMPUTER

    def run(self, ctx: ToolContext, action: str, text: str | None = None) -> ToolResult:
        try:
            import pyperclip
        except ImportError as e:
            raise ToolError("pyperclip kurulu değil") from e
        if action == "get":
            return ToolResult(pyperclip.paste() or "(pano boş)")
        pyperclip.copy(text or "")
        return ToolResult("panoya kopyalandı")


TOOLS = [Windows(), UIElements(), UIAct(), Clipboard()]
