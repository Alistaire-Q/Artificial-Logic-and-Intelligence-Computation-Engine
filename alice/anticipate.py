"""Önsezi motorunun eylemleri: alışkanlıktan ne yapacağını tahmin edip önceden hazırlar.

  * Oyundan önce (now.Anticipator, beklenen War Thunder oturumundan ~20 dk önce): anons sesleri hazırlanır (ses
    modülünün kancası) ve oyunun Steam güncellemesi kontrol edilir. Güncelleme bekliyorsa Steam arka planda
    (`-silent`, pencere açmadan) başlatılır. Oyun Steam'de "sadece oyunu açınca güncelle" ayarındaysa Steam bunu
    önceden indirmez: bunu bir kez söyler, ayarı senin yerine değiştirmez.
  * Maçta: uçuş moduna geçilince Windows "Rahatsız etmeyin" açılır (Discord dahil bütün uygulamaların bildirim
    balonları susar, sesli sohbet etkilenmez). Uçuş bitince eski haline döner; zaten açıksa dokunmaz. Windows'un bunun
    için resmi bir API'si yok: odak yardımı durumu (WNF) üzerinden yapılır, çalışmazsa sessizce vazgeçer.
  * Sabah: son haftalarda sabah ilk sorduğun şeyler (hava, program, e-posta...) kayıtlardan çıkarılır. Genelde uyandığın
    saatten önce cevapları hazırlanır, sesli asistan beklemeden söyler.

Durum ~/.alice/anticipate.json'da. [anticipate] ayarlarıyla parçalar ayrı ayrı kapatılır.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from alice.config import alice_home

WT_APPID = "236390"
DND_STATE = 0x0D83063EA3BF1C75   # WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED: 0 kapalı, 1 öncelikli, 2 yalnız alarmlar
DND_ALARMS_ONLY = 2
MORNING = (5, 12)                # sabah sayılan saatler
MIN_DAYS = 3                     # bir alışkanlık için en az bu kadar gün
MORNING_BEFORE = 20 * 60         # genelde uyandığın saatten bu kadar önce hazırla
READY_UNTIL = 13                 # hazır cevap öğleden sonra eskimiş sayılır

TOPICS = {  # sabah ilk sorulan şey -> konu
    "hava": r"\bhava\b|yağmur|sıcaklık|kaç derece|şemsiye",
    "program": r"program|takvim|bugün ne var|dersim|toplantı|staj",
    "posta": r"\bmail|e-?posta|gmail|mesaj(ım|lar)",
    "oyun": r"war thunder|güncelleme|\bdcs\b",
}


def state_path() -> Path:
    return alice_home() / "anticipate.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Durum dosyasını iki thread yazar (mod değişince QuietGame, oyun öncesi hazırlık, sabah). Kilitsiz oku-değiştir-yaz
# birinin yazdığını (ör. "Rahatsız etmeyin önceden kapalıydı" notu) ötekinin silmesine yol açıyordu: uçuştan sonra
# DND açık kalıyordu. Değişiklikler state_edit() ile kilit altında yapılır, dosya atomik değiştirilir.
_STATE_LOCK = threading.RLock()


def save_state(st: dict[str, Any], path: Path | None = None) -> None:
    p = path or state_path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:  # Windows: okuyan/virüs tarayıcı dosyayı bir an tutuyor
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


@contextmanager
def state_edit(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Durumu kilit altında oku; blok bitince (hata yoksa) aynı kilit altında atomik yaz."""
    with _STATE_LOCK:
        st = load_state(path)
        yield st
        save_state(st, path)


# ------------------------------------------------------------------ Steam
def _steam_root() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        return None


def _vdf_values(text: str, key: str) -> list[str]:
    return re.findall(rf'"{key}"\s+"([^"]*)"', text)


def steam_manifest(appid: str = WT_APPID, root: Path | None = None) -> dict[str, str] | None:
    """Oyunun Steam kayıt dosyası (appmanifest_<id>.acf) alanları; bulunamazsa None."""
    root = root or _steam_root()
    if root is None:
        return None
    libs = [root]
    try:
        text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libs += [Path(p.replace("\\\\", "\\")) for p in _vdf_values(text, "path")]
    except OSError:
        pass
    for lib in libs:
        f = lib / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return {k: (_vdf_values(text, k) or [""])[0] for k in ("name", "StateFlags", "AutoUpdateBehavior", "buildid")}
    return None


def update_state(manifest: dict[str, str] | None) -> dict[str, Any] | None:
    """{pending: güncelleme bekliyor mu, on_launch_only: Steam yalnızca oyun açılınca mı indirir}."""
    if not manifest:
        return None
    try:
        flags = int(manifest.get("StateFlags") or 0)
    except ValueError:
        return None
    return {"pending": bool(flags & 2) and not flags & 1024, "downloading": bool(flags & 1024),
            "on_launch_only": (manifest.get("AutoUpdateBehavior") or "0") == "1",
            "build": manifest.get("buildid") or "", "name": manifest.get("name") or "oyun"}


def steam_running(processes: set[str]) -> bool:
    return any(p.lower() == "steam.exe" for p in processes)


def start_steam_silent(root: Path | None = None) -> bool:
    root = root or _steam_root()
    exe = root / "steam.exe" if root else None
    if exe is None or not exe.exists():
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe), "-silent"], creationflags=flags, close_fds=True)  # noqa: S603
        return True
    except OSError:
        return False


def prep_game_update(processes: set[str], notify: Callable[[str, str], None], st: dict[str, Any],
                     manifest: dict[str, str] | None = None, start: Callable[[], bool] | None = None) -> str:
    """Oyundan önce: güncelleme bekliyorsa Steam'i arka planda aç ya da ayar gerekiyorsa bir kez söyle."""
    u = update_state(manifest if manifest is not None else steam_manifest())
    if not u or not (u["pending"] or u["downloading"]):
        return ""
    if u["downloading"]:
        return f"{u['name']} güncellemesi Steam'de iniyor."
    if u["on_launch_only"]:
        if st.get("update_told") != u["build"]:
            st["update_told"] = u["build"]
            notify(f"{u['name']} güncellemesi bekliyor",
                   "Steam'de bu oyun 'sadece oyunu açınca güncelle' ayarında, o yüzden güncelleme oyunu açınca inecek. "
                   "Steam → War Thunder → Özellikler → Güncellemeler → 'Bu oyunu her zaman güncel tut' seçersen "
                   "oyundan önce ben indirtirim.")
        return f"{u['name']} güncellemesi oyun açılınca inecek (Steam ayarı)."
    if not steam_running(processes) and (start or start_steam_silent)():
        return f"{u['name']} güncellemesi için Steam arka planda açıldı."
    return f"{u['name']} güncellemesi Steam'de bekliyor."


# ------------------------------------------------------------ rahatsız etmeyin
def _wnf() -> tuple[Callable[[], int | None], Callable[[int], bool]] | None:
    if sys.platform != "win32":
        return None
    import ctypes

    nt = ctypes.windll.ntdll
    name = ctypes.c_ulonglong(DND_STATE)

    def read() -> int | None:
        stamp, size = ctypes.c_ulong(0), ctypes.c_ulong(4)
        buf = ctypes.c_uint32(0)
        st = nt.NtQueryWnfStateData(ctypes.byref(name), None, None, ctypes.byref(stamp), ctypes.byref(buf),
                                    ctypes.byref(size))
        return int(buf.value) if st == 0 and size.value == 4 else None

    def write(value: int) -> bool:
        buf = ctypes.c_uint32(value)
        st = nt.NtUpdateWnfStateData(ctypes.byref(name), ctypes.byref(buf), 4, None, None, 0, 0)
        return st == 0 and read() == value

    return read, write


class QuietGame:
    """Uçuş modunda Windows "Rahatsız etmeyin"; uçuş bitince eski haline. Kendi açtığını hatırlar (çekirdek
    çökse bile bir sonraki açılışta geri alır)."""

    def __init__(self, enabled: bool = True, wnf: tuple[Callable[[], int | None], Callable[[int], bool]] | None = None,
                 path: Path | None = None):
        self.enabled = enabled
        self.path = path
        self.wnf = wnf if wnf is not None else (_wnf() if enabled else None)

    def mode(self, mode: str) -> str:
        """Mod değişince çağrılır; ne yaptığını döndürür ("" = hiçbir şey)."""
        if not self.enabled or self.wnf is None:
            return ""
        read, write = self.wnf
        with _STATE_LOCK:  # oku-değiştir-yaz arasında başka thread dnd_prev'i silmesin
            st = load_state(self.path)
            if mode == "flight":
                cur = read()
                if cur is None or cur != 0 or "dnd_prev" in st:
                    return ""  # okunamadı, zaten açık ya da zaten biz açtık
                if write(DND_ALARMS_ONLY):
                    st["dnd_prev"] = cur
                    save_state(st, self.path)
                    return "Rahatsız etmeyin açıldı"
                return ""
            if "dnd_prev" in st:
                try:
                    prev = int(st.pop("dnd_prev"))
                except (TypeError, ValueError):
                    prev = 0
                save_state(st, self.path)
                if read() == DND_ALARMS_ONLY:  # sen bu arada değiştirdiysen dokunma
                    write(prev)
                    return "Rahatsız etmeyin eski haline döndü"
        return ""


# --------------------------------------------------------------------- sabah
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2} .*?duyulan(?: \[[^\]]*\])?: (.*?)(?: \| cevap:|$)")


def morning_habits(lines: list[str]) -> tuple[Counter, list[int]]:
    """voice.log satırları -> (sabah ilk sorulan konuların sayısı, her günün ilk sesli etkinliği dakika olarak)."""
    first: dict[str, tuple[int, str]] = {}
    for ln in lines:
        m = _LINE.match(ln)
        if not m or not m.group(4).strip():
            continue
        day, h, mi, text = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).strip()
        if MORNING[0] <= h < MORNING[1] and day not in first:
            first[day] = (h * 60 + mi, text)
    topics: Counter = Counter()
    for _minute, text in first.values():
        low = text.lower()
        for name, rx in TOPICS.items():
            if re.search(rx, low):
                topics[name] += 1
                break
    return topics, [m for m, _ in first.values()]


def _read_log_tail(path: Path, max_bytes: int = 4_000_000) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def session_lines(folder: Path | None = None, limit: int = 300) -> list[str]:
    """Yazılı sohbetlerin ilk sorusu, voice.log satırı biçiminde (dosya adı başlangıç zamanı: 20261102-074512)."""
    folder = folder or alice_home() / "sessions"
    out = []
    for f in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$", f.stem)
        if not m:
            continue
        try:
            msgs = json.loads(f.read_text(encoding="utf-8")).get("messages") or []
        except (OSError, ValueError, AttributeError):
            continue
        first = next((x.get("content") for x in msgs if x.get("role") == "user" and isinstance(x.get("content"), str)),
                     "")
        if first:
            y, mo, d, h, mi, s = m.groups()
            out.append(f"{y}-{mo}-{d} {h}:{mi}:{s} sohbet duyulan: {' '.join(first.split())[:200]}")
    return sorted(out)


def morning_plan(lines: list[str]) -> tuple[list[str], int | None]:
    """(hazırlanacak konular, hazırlık saati dakika olarak). Yeterli geçmiş yoksa ([], None)."""
    topics, starts = morning_habits(lines)
    if len(starts) < MIN_DAYS:
        return [], None
    wanted = [t for t, n in topics.most_common() if n >= MIN_DAYS]
    if not wanted:
        return [], None
    return wanted, max(MORNING[0] * 60, int(statistics.median(starts)) - MORNING_BEFORE // 60)


def prepare_morning(cfg: Any, topics: list[str], now: datetime,
                    fetch: dict[str, Callable[[], str]] | None = None) -> dict[str, str]:
    """Konuların cevaplarını şimdi hazırla. Hata veren konu atlanır."""
    fetch = fetch if fetch is not None else _fetchers(cfg, now)
    out: dict[str, str] = {}
    for t in topics:
        fn = fetch.get(t)
        if fn is None:
            continue
        try:
            text = " ".join(str(fn() or "").split())
        except Exception:  # noqa: BLE001 - bir kaynak düşse de diğerleri hazırlansın
            continue
        if text:
            out[t] = text[:300]
    return out


def _fetchers(cfg: Any, now: datetime) -> dict[str, Callable[[], str]]:
    from alice.extras import brief

    b = dict((getattr(cfg, "data", None) or {}).get("brief") or {})

    def program() -> str:
        return str(brief.gather(b, now=now, weather_fn=lambda c: "").get("bugün") or "")

    def posta() -> str:
        from alice.presence import unread_mail

        mails = unread_mail(cfg)
        return f"{len(mails)} önemli okunmamış e-posta: " + "; ".join(mails[:3]) if mails else "önemli yeni e-posta yok"

    def oyun() -> str:
        u = update_state(steam_manifest())
        if not u:
            return ""
        return f"{u['name']} güncellemesi bekliyor" if u["pending"] else f"{u['name']} güncel"

    return {"hava": lambda: brief.weather(str(b.get("city") or ""), auto=True), "program": program,
            "posta": posta, "oyun": oyun}


def ready_line(now: datetime | None = None, path: Path | None = None) -> str:
    """Bugün için hazırlanmış sabah cevapları (sesli asistanın bağlamına eklenir); yoksa ya da eskidiyse boş."""
    now = now or datetime.now()
    st = load_state(path)
    ready = st.get("morning") or {}
    if ready.get("day") != f"{now:%Y-%m-%d}" or now.hour >= READY_UNTIL:
        return ""
    answers = ready.get("answers") or {}
    return "; ".join(f"{k}: {v}" for k, v in answers.items())


class Morning:
    """Çekirdek turunda: sabah hazırlık saati geldiyse günde bir kez hazırla."""

    def __init__(self, cfg: Any, log: Path | None = None, path: Path | None = None,
                 fetch: dict[str, Callable[[], str]] | None = None):
        self.cfg, self.log, self.path, self.fetch = cfg, log or alice_home() / "voice.log", path, fetch
        self._plan: tuple[list[str], int | None] | None = None
        self._plan_day = ""

    def check(self, now: datetime | None = None) -> dict[str, str] | None:
        now = now or datetime.now()
        day = f"{now:%Y-%m-%d}"
        st = load_state(self.path)
        if (st.get("morning") or {}).get("day") == day:
            return None
        if self._plan_day != day:
            lines = sorted(_read_log_tail(self.log) + session_lines(self.log.parent / "sessions"))  # sesli + yazılı
            self._plan, self._plan_day = morning_plan(lines), day
        topics, at = self._plan or ([], None)
        if not topics or at is None or not at <= now.hour * 60 + now.minute < MORNING[1] * 60:
            return None
        answers = prepare_morning(self.cfg, topics, now, self.fetch)
        with state_edit(self.path) as st:
            st["morning"] = {"day": day, "at": time.time(), "answers": answers}
        return answers
