"""Ekrandan füze görme: War Thunder füze ve RWR bilgisini API'de vermiyor ama ekranda gösteriyor.

Sadece uçuşta, ekranın yalnızca RWR göstergesinin olduğu küçük bölgesi işlemcide saniyede 10 kez yakalanır (mss).
Tanıma kurallarla yapılır: kırmızı/turuncu işaretlerin renk eşiği, dairenin merkezine göre açıdan saat yönü.
Yanıp sönen uyarı birkaç karede doğrulanır, tek karelik yanlış alarm olmaz. Sonuç temas listesine `missile: True`
olarak eklenir; bugünkü "Füze! Saat 4" anonsu ve HUD oku değişiklik gerekmeden çalışır.

Oyuna dokunmaz, belleğini okumaz: OBS'in yaptığı gibi ekran görüntüsü alır. Ekran kartı kullanılmaz.

  alice vision region X Y G Y    RWR bölgesini ayarla (fiziksel piksel), işaretli görüntüyü kaydet
  alice vision show              ayarlı bölgeyi tam ekran görüntüsünde çerçeveleyip kaydet (onay için)
  alice vision record            kısayolla örnek topla: Ctrl+Alt+M füze var, Ctrl+Alt+N yok
  alice vision test DOSYA...     kayıtlı görüntülerde tanımayı dene
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home

DEFAULTS: dict[str, Any] = {
    "method": "text",     # text: War Thunder'ın turkuaz RWR yazıları (LAUNCH/TRACK/MSL); red: eski kırmızı işaret
    "fps": 6.0,           # LAUNCH saniyede ~2 kez yanıp sönüyor: 2 kare/sn hep sönük ana denk gelebiliyordu
    "red_min": 170,       # kırmızı kanal en az
    "red_margin": 70,     # kırmızı, yeşil ve maviden en az bu kadar fazla (turuncu dahil, beyaz/gri hariç)
    "min_pixels": 12,     # bundan az işaretli piksel gürültü sayılır
    "center_ignore": 0.18,  # dairenin ortası (kendi uçağın simgesi) yarıçapın bu oranı kadar yok sayılır
    "confirm": 2,         # son 'window' karenin en az bu kadarında görülmeli (yanıp sönen uyarı: yarısı yeter)
    "window": 5,
    "hold": 1.5,          # doğrulanmış uyarı bu kadar görünmezse biter (sn)
}
IDLE_AFTER = 3.0  # uçuş verisi bu kadar gelmezse yakalama durur (hangar, ölüm)


def samples_dir() -> Path:
    return alice_home() / "vision"


@dataclass
class Detection:
    clock: int
    pixels: int


def detect(rgb, cfg: dict[str, Any] | None = None) -> Detection | None:
    """Tek kare (yükseklik x genişlik x 3, RGB) -> füze/RWR işareti ve saat yönü. Yoksa None."""
    import numpy as np

    c = {**DEFAULTS, **(cfg or {})}
    a = np.asarray(rgb, dtype=np.int16)
    if a.ndim != 3 or a.shape[2] < 3 or a.shape[0] < 4 or a.shape[1] < 4:
        return None
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = int(c["red_margin"])
    mask = (r >= int(c["red_min"])) & (r - g >= m) & (r - b >= m)
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    cx, cy = (w - 1) / 2, (h - 1) / 2
    dx, dy = xs - cx, ys - cy
    radius = min(w, h) / 2
    keep = np.hypot(dx, dy) >= radius * float(c["center_ignore"])
    dx, dy = dx[keep], dy[keep]
    if len(dx) < int(c["min_pixels"]):
        return None
    ang = math.degrees(math.atan2(float(dx.mean()), float(-dy.mean()))) % 360  # 0 yukarı, saat yönünde
    clock = round(ang / 30) % 12 or 12
    return Detection(clock, int(len(dx)))


# War Thunder'ın RWR'ı (Medal kliplerinden, 2026-11-03): kırmızı değil turkuaz; dairenin üstünde önce "TRACK"
# (kilitlendin), füze atılınca "LAUNCH" kutusu çıkar, dairenin içinde füzenin yönünde "MSL" yazısı belirir. 1080 piksel
# yükseklikte ekranın sol kenarında: daire merkezi (187, 539), yarıçap ~113; bölge etiket şeridini de içerir.
# HUD yüksekliğe göre ölçeklendiği için başka çözünürlükte hepsi yükseklik/1080 ile çarpılır.
WT_REGION_1080 = (60, 380, 255, 285)       # x, y, genişlik, yükseklik
WT_CENTER = (0.498, 0.615)                 # dairenin merkezi (kendi uçağın simgesi), bölgenin kesri
OCR_UPSCALE = 2                            # küçük HUD yazısı büyütülünce daha iyi okunur


_TR = str.maketrans("ŞşĞğÇçÜüİıÖö$5", "SsGgCcUuIiOoSS")
MSL_RX = __import__("re").compile(r"MS[LIU1]|M[A-Z]L\b|[A-Z]SL")  # okuma motoru MSL'yi "MSÜ", "MğL" yazabiliyor


def _plain(text: str) -> str:
    """Türkçe okuma motorunun harflerini sadeleştir: "MSÜ" -> "MSU", "LAUNŞH" -> "LAUNSH"."""
    return str(text).translate(_TR).upper()


def _like(text: str, word: str) -> bool:
    """Yazı bu kelimeye yeterince benziyor mu (okuma motoru bir harfi karıştırabiliyor)."""
    import difflib

    return word in text or any(difflib.SequenceMatcher(None, text[i:i + len(word)], word).ratio() >= 0.75
                               for i in range(max(1, len(text) - len(word) + 1)))


def hud_mask(img):
    """Sadece War Thunder'ın turkuaz HUD rengini bırak (beyaz zeminde siyah yazı): ağaç ve gökyüzü üstündeki
    yazıyı okuma motoru böyle çok daha iyi okuyor ("LAUNCH" ham görüntüde hiç okunmadı, maskeyle okundu)."""
    import numpy as np
    from PIL import Image

    a = np.asarray(img.convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = (g > 150) & (b > 120) & (r < 140) & (g - r > 70)
    return Image.fromarray(np.where(m, 0, 255).astype(np.uint8))


def default_region(screen_h: int) -> tuple[int, int, int, int]:
    f = screen_h / 1080
    return tuple(round(v * f) for v in WT_REGION_1080)  # type: ignore[return-value]


@dataclass
class TextDetection:
    clock: int | None   # MSL görülmediyse None: yönsüz uyarı (HUD radar simgesi gösterir)
    launch: bool        # füze atıldı ("LAUNCH" ya da "MSL")
    track: bool         # kilitlenme ("TRACK")
    pixels: int = 0


def read_rwr(rgb, cfg: dict[str, Any] | None = None, words=None) -> TextDetection | None:
    """RWR bölgesindeki yazılardan füze/kilit ve yön. words: testte OCR yerine (kelime, (x, y, en, boy))."""
    from PIL import Image

    import numpy as np

    c = {**DEFAULTS, **(cfg or {})}
    img = Image.fromarray(np.asarray(rgb).astype("uint8")) if not isinstance(rgb, Image.Image) else rgb
    k = OCR_UPSCALE
    if words is None:
        from alice.reflex.perceive import ocr_words

        big = hud_mask(img).resize((img.width * k, img.height * k), Image.LANCZOS)
        words = [(t, (x / k, y / k, w / k, h / k)) for t, (x, y, w, h) in ocr_words(big)]
    up = [(_plain(t), box) for t, box in words]
    launch = any(_like(t, "LAUNCH") for t, _ in up)
    track = any(_like(t, "TRACK") for t, _ in up)
    seen_msl = any(MSL_RX.search(t) for t, _ in up)
    # Yön yalnız tek başına okunan kısa "MSL"den: motor bazen yanındaki işaretle birleştiriyor ("M29MSL"),
    # birleşik kelimenin ortası füzenin yerini göstermiyor (kliplerde saat 12 -> 6 -> 1 atlıyordu).
    msl = [box for t, box in up if MSL_RX.search(t) and len(t.strip(".,:;()'\"")) <= 4]
    if not (launch or track or seen_msl):
        return None
    clock = None
    if msl:
        cx, cy = (float(v) for v in c.get("rwr_center", WT_CENTER))
        x, y, w, h = msl[0]
        dx = (x + w / 2) - cx * img.width
        dy = (y + h / 2) - cy * img.height
        if (dx * dx + dy * dy) ** 0.5 >= 4:  # tam ortada: yön belirsiz
            ang = math.degrees(math.atan2(dx, -dy)) % 360  # 0 yukarı (burun), saat yönünde
            clock = round(ang / 30) % 12 or 12
    return TextDetection(clock, launch or seen_msl, track)


class Confirmer:
    """Yanıp sönen uyarıyı doğrular: son 'window' karenin en az 'confirm' tanesinde işaret olmalı."""

    def __init__(self, confirm: int = 3, window: int = 5, hold: float = 1.5):
        self.confirm, self.hold = confirm, hold
        self.frames: deque[Detection | None] = deque(maxlen=window)
        self.last: Detection | None = None
        self.seen_at = -1e9

    def feed(self, d: Detection | None, now: float) -> Detection | None:
        self.frames.append(d)
        hits = [x for x in self.frames if x is not None]
        if len(hits) >= self.confirm:
            self.last, self.seen_at = hits[-1], now
        if self.last is not None and now - self.seen_at > self.hold:
            self.last = None
        return self.last


_local = threading.local()


def grab_region(region: tuple[int, int, int, int]):
    """Ekranın yalnızca bu küçük bölgesi (x, y, genişlik, yükseklik) -> RGB dizi. mss nesnesi iş parçacığı başına bir
    kez açılır: her karede yeniden açmak bir çekirdeğin %12'sini yiyordu."""
    import mss
    import numpy as np

    sct = getattr(_local, "sct", None)
    if sct is None:
        from alice.tools.computer import _dpi_aware

        _dpi_aware()
        sct = _local.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
    x, y, w, h = region
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(h, w, 4)[..., 2::-1]  # BGRA -> RGB


class MissileWatcher:
    """Uçuş sürdükçe RWR bölgesini izler; contacts() doğrulanmış füze uyarısını temas olarak verir."""

    def __init__(self, region: tuple[int, int, int, int], cfg: dict[str, Any] | None = None,
                 grab: Callable[[tuple[int, int, int, int]], Any] = grab_region):
        self.region = region
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.grab = grab
        self.confirmer = Confirmer(int(self.cfg["confirm"]), int(self.cfg["window"]), float(self.cfg["hold"]))
        self.locker = Confirmer(int(self.cfg["confirm"]), int(self.cfg["window"]), float(self.cfg["hold"]))
        self._lock_cur: Any = None
        self._poked = -1e9
        self._current: Detection | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors = 0

    def poke(self) -> None:
        """Uçuş verisi geldi: yakalama sürsün (ya da başlasın)."""
        self._poked = time.monotonic()
        self._wake.set()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="alice-missile-vision")
            self._thread.start()

    def step(self, now: float | None = None) -> Detection | None:
        now = time.monotonic() if now is None else now
        lock = None
        try:
            frame = self.grab(self.region)
            if self.cfg.get("method", "text") == "text":
                t = read_rwr(frame, self.cfg)
                d = t if t is not None and t.launch else None
                lock = t if t is not None and t.track else None
            else:
                d = detect(frame, self.cfg)
        except Exception:  # noqa: BLE001 - yakalama/okuma hatası (ekran kilitli, özel tam ekran) uçuşu bozmasın
            self.errors += 1
            d = None
        cur = self.confirmer.feed(d, now)
        lk = self.locker.feed(lock, now)
        with self._lock:
            self._current, self._lock_cur = cur, lk
        return cur

    def _run(self) -> None:
        every = 1.0 / max(1.0, float(self.cfg["fps"]))
        while not self._stop.is_set():
            if time.monotonic() - self._poked > IDLE_AFTER:  # hangar, ölüm: ekran yakalanmaz
                with self._lock:
                    self._current = None
                self._wake.clear()
                self._wake.wait(5)
                continue
            t0 = time.monotonic()
            self.step(t0)
            self._stop.wait(max(0.0, every - (time.monotonic() - t0)))

    def contacts(self) -> list[dict[str, Any]]:
        with self._lock:
            d, lk = self._current, self._lock_cur
        if d is not None:  # füze (yönü bilinmiyorsa clock None: anons yönsüz, HUD radar simgesi)
            return [{"id": "vision", "name": "", "kind": "radar", "clock": d.clock, "lock": True, "missile": True,
                     "dist_m": None, "vision": True}]
        if lk is not None:  # kilitlendin ama füze yok
            return [{"id": "vision", "name": "", "kind": "radar", "clock": lk.clock, "lock": True, "missile": False,
                     "dist_m": None, "vision": True}]
        return []

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def region_of(cfg: dict[str, Any]) -> tuple[int, int, int, int] | None:
    r = cfg.get("vision_region")
    if isinstance(r, (list, tuple)) and len(r) == 4:
        try:
            x, y, w, h = (int(v) for v in r)
        except (TypeError, ValueError):
            return None
        if w >= 8 and h >= 8:
            return x, y, w, h
    return None


# ---------------------------------------------------------------------- komut
def _full_screenshot():
    import mss
    import numpy as np

    from alice.tools.computer import _dpi_aware

    _dpi_aware()
    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        left, top = sct.monitors[0]["left"], sct.monitors[0]["top"]
    return np.asarray(shot)[..., [2, 1, 0]], (left, top)


def save_marked(region: tuple[int, int, int, int], out: Path) -> Path:
    """Tam ekran görüntüsünde bölgeyi yeşil çerçeveyle işaretle (bölge doğru mu, gözle onay)."""
    from PIL import Image, ImageDraw

    rgb, (left, top) = _full_screenshot()
    img = Image.fromarray(rgb.astype("uint8"))
    x, y, w, h = region
    ImageDraw.Draw(img).rectangle([x - left, y - top, x - left + w, y - top + h], outline=(0, 255, 0), width=4)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def test_files(paths: list[Path], region: tuple[int, int, int, int] | None, cfg: dict[str, Any]) -> list[str]:
    """Kayıtlı görüntülerde tanıma: tam ekran görüntüsüyse ayarlı bölge kesilir."""
    import numpy as np
    from PIL import Image

    out = []
    for p in paths:
        a = np.asarray(Image.open(p).convert("RGB"))
        if region is not None and a.shape[0] > region[3] and a.shape[1] > region[2]:
            x, y, w, h = region
            a = a[y:y + h, x:x + w]
        d = detect(a, cfg)
        out.append(f"{p.name}: " + (f"işaret var, saat {d.clock} ({d.pixels} piksel)" if d else "işaret yok"))
    return out


def record(cfg: dict[str, Any], seconds: float = 3600.0) -> int:
    """Kısayolla örnek topla. Etiket dosya adında: missile-*.png / clear-*.png."""
    from PIL import Image

    from alice.hotkeys import WinHotkeys

    folder = samples_dir()
    folder.mkdir(parents=True, exist_ok=True)

    def save(label: str) -> None:
        rgb, _ = _full_screenshot()
        p = folder / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        Image.fromarray(rgb.astype("uint8")).save(p)
        print(f"kaydedildi: {p}")

    keys = WinHotkeys({"<ctrl>+<alt>+m": lambda: save("missile"), "<ctrl>+<alt>+n": lambda: save("clear")})
    if not keys.start():
        print("kısayollar alınamadı (başka bir program tutuyor olabilir)")
        return 1
    print(f"Örnek toplama açık ({folder}). Füze uyarısı ekrandayken Ctrl+Alt+M, uyarı yokken Ctrl+Alt+N. "
          "Bitince Ctrl+C.")
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        keys.stop()
    return 0


def vision_cmd(argv: list[str]) -> int:
    from alice.config import load_config, set_config_value

    sims = load_config().data.get("sims", {}) or {}
    sub = argv[0] if argv else ""
    region = region_of(sims)
    cfg = {**DEFAULTS, **(sims.get("vision") or {})}
    if sub == "region" and len(argv) == 5:
        try:
            r = [int(v) for v in argv[1:5]]
        except ValueError:
            print("kullanım: alice vision region X Y GENİŞLİK YÜKSEKLİK")
            return 2
        set_config_value("sims", "vision_region", r)
        p = save_marked(tuple(r), samples_dir() / "bolge.png")  # type: ignore[arg-type]
        print(f"bölge kaydedildi: {r}. Doğru yer mi bak: {p}")
        return 0
    if sub == "show":
        if region is None:
            print("bölge ayarlı değil: alice vision region X Y GENİŞLİK YÜKSEKLİK")
            return 2
        print(f"işaretli görüntü: {save_marked(region, samples_dir() / 'bolge.png')}")
        return 0
    if sub == "record":
        return record(cfg)
    if sub == "test" and len(argv) > 1:
        files = [Path(a) for a in argv[1:]]
        if len(files) == 1 and files[0].is_dir():
            files = sorted(files[0].glob("*.png"))
        for line in test_files(files, region, cfg):
            print(line)
        return 0
    print(__doc__)
    return 2
