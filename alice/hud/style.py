"""HUD'un görünümü. Apple'ın Dynamic Island ve Live Activities kurallarına göre (developer.apple.com/design/human-
interface-guidelines/live-activities, /materials, /color):

  * Tek yüzey, "Ada": ekranın üst ortasında tek bir kapsül. Tehdit, yakıt, sıcaklık, bildirim, konuşma hep onun
    içinde; birden fazla şey varsa en önemlisi Adada, diğerleri yanında küçük dairelerde (minimal görünüm). Ayrı
    ayrı yüzen paneller yok: HIG yarı saydam katmanları üst üste koymamayı ve tek yüzeyde dönüşümlü göstermeyi
    söylüyor.
  * apple.com'un açık dili: beyaz-gri buzlu cam (arkasındaki ekran gerçekten bulanıklaşır, üstünde açık tül, kılcal
    kenar ve üst kenarda ışık), koyu yazı #1D1D1F, ikincil #6E6E73, marka mavisi #0071E3. Siyah Ada ve doygun
    turuncu "oyun arayüzü" gibi duruyordu. Renk sadece simge ve değerde, sadece anlam için; kritikte ince kırmızı
    çizgi ve yavaş nefes. Renkli nokta, renkli çerçeve yok.
  * Yazı büyük ve en az orta kalınlıkta (HIG: "large, heavier-weight text"). Segoe UI Variable'ın Display/Text optik
    boyutları SF'nin Windows'taki en yakın karşılığı. Simgeler SF Symbols gibi tek renk, yazıyla aynı ağırlıkta.
  * Eş merkezli kenar boşlukları: içerik kenara sıkı, iç köşeler dış köşeyle uyumlu; köşeler Apple'ın sürekli köşesi
    (squircle, n=5 süper elips).
  * Hareket: Ada boyut değiştirirken yay gibi büyür/küçülür, içerik değişince eskisi söner yenisi belirir; hiçbir
    animasyon 2 sn'yi geçmez.

Ada dışında yalnızca iki şey çizilir: kritik tehdidin yönünde ekran kenarında yumuşak ışık, tıklanacak yerin etrafında
macOS odak halkası. Bu modül Windows'a dokunmaz; pencereye basmak window.py'nin işi.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from alice.hud.model import Item, clock_of

# apple.com'un açık dili: beyaz-gri buzlu cam, koyu yazı, marka mavisi. Durum renkleri HIG açık mod "artırılmış
# kontrast" değerleri: açık camın üstünde yazı olarak okunsunlar (normal turuncu beyazda soluk kalıyordu).
RED, ORANGE, YELLOW = (233, 21, 45), (197, 83, 0), (161, 106, 0)
GREEN, BLUE, GRAY = (0, 137, 50), (0, 113, 227), (142, 142, 147)
GLOW = {"info": (0, 136, 255), "warn": (255, 141, 40), "critical": (255, 56, 60)}  # kenar ışığı: canlı tonlar
COLORS = {"info": BLUE, "warn": ORANGE, "critical": RED}
LABEL = (29, 29, 31)             # apple.com ana yazı (#1D1D1F)
LABEL_2 = (110, 110, 115, 255)   # apple.com ikincil yazı (#6E6E73)
TINT = (250, 250, 252, 172)      # buzlu camın tülü (arkası bulanık ekran)
TINT_SOLID = (245, 245, 247, 240)  # arka plan alınamazsa (deneme, eski Windows): neredeyse opak açık gri
HAIRLINE = (0, 0, 0, 24)         # camı zeminden ayıran kılcal çizgi
HIGHLIGHT = (255, 255, 255, 190)  # üst kenardaki ışık yansıması (cam kalınlığı hissi)
TRACKING = -0.022                # em: apple.com'da 17 px yazıda -0,374 px; sıkı, tok görünüm
SS = 3                           # kenar yumuşatma için büyütme

FADE_IN, FADE_OUT, TURN = 0.22, 0.18, 0.2   # sn
PULSE = 1.6                                 # sn: kritik nefes
PRIORITY = {"critical": 0, "warn": 1, "info": 2}


def scale_for(screen_h: int) -> float:
    """1080p'de 1; 1440p'de 1,33; 4K'da 2."""
    return max(1.0, min(2.0, screen_h / 1080))


# ------------------------------------------------------------------ yazı
@lru_cache(maxsize=64)
def font(size: int, weight: int = 600, display: bool = True) -> ImageFont.FreeTypeFont:
    """Segoe UI Variable: weight 300-700, optik boyut (display büyük başlık, text gövde). Yoksa Segoe UI."""
    for path in ("C:/Windows/Fonts/SegUIVar.ttf",):
        try:
            f = ImageFont.truetype(path, size)
            f.set_variation_by_axes([weight, 36 if display else 10])
            return f
        except (OSError, ValueError, AttributeError):
            continue
    for name in (("seguisb.ttf", "segoeuib.ttf") if weight >= 600 else ("segoeui.ttf",)) + ("arial.ttf",):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _tw(s: str, f: ImageFont.FreeTypeFont, tracking: float = TRACKING) -> int:
    """Harf aralığıyla birlikte yazı genişliği."""
    return int(math.ceil(f.getlength(s) + tracking * f.size * max(0, len(s) - 1)))


def text(d: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, f: ImageFont.FreeTypeFont, fill: tuple,
         anchor: str = "lm", tracking: float = TRACKING) -> None:
    """Negatif harf aralıklı yazı (PIL'de yok: harf harf, çiftlerin kendi aralığı korunarak çizilir).
    anchor: l/r/m yatay + m/a dikey (lm, rm, la)."""
    if not s:
        return
    width = _tw(s, f, tracking)
    x, y = xy
    if anchor[0] == "r":
        x -= width
    elif anchor[0] == "m":
        x -= width / 2
    v = anchor[1]
    pos = x
    for i, ch in enumerate(s):
        d.text((pos, y), ch, font=f, fill=fill, anchor="l" + v)
        pair = f.getlength(s[i:i + 2]) - f.getlength(s[i + 1]) if i + 1 < len(s) else f.getlength(ch)
        pos += pair + tracking * f.size


def tr_sentence(s: str) -> str:
    """'ARKANDA · 900 m' -> 'Arkanda · 900 m' (Türkçe İ/ı ile). Büyük harfle bağırmak Apple dili değil."""
    def low(w: str) -> str:
        return w.replace("I", "ı").replace("İ", "i").lower()

    parts = s.split(" ")
    out = []
    for p in parts:
        letters = [c for c in p if c.isalpha()]
        # kısaltmalar (SAM, GPU, DCS) kalır; bağıran kelimeler (ARKANDA, YAKIT) küçülür
        out.append(low(p) if len(letters) > 3 and all(c.isupper() for c in letters) else p)
    t = " ".join(out)
    if t and t[0] == "i":
        return "İ" + t[1:]
    return t[:1].upper() + t[1:]


def _wrap(s: str, f: ImageFont.FreeTypeFont, width: int, max_lines: int = 3) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if cur and _tw(t, f) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _tw(last + "…", f) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# ------------------------------------------------------------------ şekil
def squircle(box: tuple[float, float, float, float], r: float, n: float = 5.0, steps: int = 14) -> list[tuple]:
    """Apple'ın sürekli köşeli dikdörtgeni: köşeler süper elips (n=5), dairesel köşeden daha yumuşak geçer.
    r yükseklik/2'ye eşitse kapsül olur."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 0.5:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if r >= min(w, h) / 2 - 0.5:  # kapsül/daire: uçlar tam yarım daire (süper elips burada kutu gibi duruyordu)
        n, e = 2.0, min(w, h) / 2
    else:
        e = min(r * 1.28, w / 2, h / 2)  # süper elips köşe, aynı görünen yarıçap için biraz daha uzun
    pts = []
    for cx, cy, a0 in ((x1 - e, y0 + e, -90), (x1 - e, y1 - e, 0), (x0 + e, y1 - e, 90), (x0 + e, y0 + e, 180)):
        for i in range(steps + 1):
            a = math.radians(a0 + 90 * i / steps)
            ca, sa = math.cos(a), math.sin(a)
            px = math.copysign(abs(ca) ** (2 / n), ca) * e
            py = math.copysign(abs(sa) ** (2 / n), sa) * e
            pts.append((cx + px, cy + py))
    return pts


def _big(size: tuple[int, int]) -> Image.Image:
    return Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))


def _down(big: Image.Image, size: tuple[int, int]) -> Image.Image:
    return big.resize(size, Image.LANCZOS)


def _shape(img: Image.Image, box: tuple, r: float, fill: tuple, outline: tuple | None = None, width: float = 1.0) -> None:
    big = _big(img.size)
    pts = [(x * SS, y * SS) for x, y in squircle(box, r)]
    d = ImageDraw.Draw(big)
    d.polygon(pts, fill=fill)
    if outline:
        d.line(pts + [pts[0]], fill=outline, width=max(1, round(width * SS)), joint="curve")
    img.alpha_composite(_down(big, img.size))


def _shadow(img: Image.Image, box: tuple, r: float, k: float, alpha: int = 120) -> None:
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(sh).polygon(squircle((x0, y0 + 6 * k, x1, y1 + 6 * k), r), fill=(0, 0, 0, alpha))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(12 * k)))


# ------------------------------------------------------------------ simgeler (SF Symbols gibi, tek renk)
def glyph(img: Image.Image, name: str, cx: float, cy: float, s: float, rgb: tuple, angle: float = 0.0) -> None:
    """s: simgenin kutu boyu (yazı yüksekliği kadar). Hepsi 3 kat çizilip küçültülür."""
    big = _big(img.size)
    d = ImageDraw.Draw(big)
    col = (*rgb, 255)
    X = lambda v: v * SS  # noqa: E731
    h = s / 2
    if name == "arrow":  # location.north.fill: yönü olan dolu ok
        dx, dy = math.sin(math.radians(angle)), -math.cos(math.radians(angle))
        px, py = -dy, dx
        shape = ((0.0, -1.0), (0.56, 0.7), (0.0, 0.34), (-0.56, 0.7))  # ucu kanatlardan uzak: yönü belli
        d.polygon([(X(cx + (px * a - dx * b) * h), X(cy + (py * a - dy * b) * h)) for a, b in shape], fill=col)
    elif name == "warning":  # exclamationmark.triangle.fill
        tri = [(cx, cy - h * 0.95), (cx + h, cy + h * 0.8), (cx - h, cy + h * 0.8)]
        d.polygon([(X(x), X(y)) for x, y in tri], fill=col)
        d.line([(X(cx), X(cy - h * 0.35)), (X(cx), X(cy + h * 0.25))], fill=(255, 255, 255, 255), width=round(X(s * 0.11)))
        r = s * 0.07
        d.ellipse([X(cx - r), X(cy + h * 0.5 - r), X(cx + r), X(cy + h * 0.5 + r)], fill=(255, 255, 255, 255))
    elif name == "drop":  # drop.fill (yakıt)
        r = h * 0.62
        d.ellipse([X(cx - r), X(cy + h * 0.3 - r), X(cx + r), X(cy + h * 0.3 + r)], fill=col)
        d.polygon([(X(cx), X(cy - h)), (X(cx + r * 0.92), X(cy + h * 0.12)), (X(cx - r * 0.92), X(cy + h * 0.12))],
                  fill=col)
    elif name == "thermometer":
        w = s * 0.2
        d.rounded_rectangle([X(cx - w / 2), X(cy - h), X(cx + w / 2), X(cy + h * 0.4)], radius=X(w / 2), fill=col)
        r = s * 0.24
        d.ellipse([X(cx - r), X(cy + h * 0.55 - r), X(cx + r), X(cy + h * 0.55 + r)], fill=col)
    elif name == "check":  # checkmark.circle.fill
        d.ellipse([X(cx - h), X(cy - h), X(cx + h), X(cy + h)], fill=col)
        pts = [(cx - h * 0.45, cy + h * 0.02), (cx - h * 0.1, cy + h * 0.38), (cx + h * 0.5, cy - h * 0.35)]
        d.line([(X(x), X(y)) for x, y in pts], fill=(255, 255, 255, 255), width=round(X(s * 0.13)), joint="curve")
    elif name == "cursor":  # cursorarrow
        pts = [(0, -1), (0.0, 0.62), (0.3, 0.32), (0.55, 0.92), (0.72, 0.84), (0.48, 0.25), (0.88, 0.25)]
        d.polygon([(X(cx - h * 0.4 + a * h), X(cy + b * h)) for a, b in pts], fill=col)
    elif name == "gauge":  # gauge.with.needle: halka
        d.arc([X(cx - h), X(cy - h), X(cx + h), X(cy + h)], 150, 390, fill=col, width=round(X(s * 0.13)))
        d.line([(X(cx), X(cy)), (X(cx + h * 0.55), X(cy - h * 0.4))], fill=col, width=round(X(s * 0.12)))
    else:  # sparkle: ALICE'in işareti (dört köşeli yıldız)
        k = 0.22
        pts = [(0, -1), (k, -k), (1, 0), (k, k), (0, 1), (-k, k), (-1, 0), (-k, -k)]
        d.polygon([(X(cx + a * h), X(cy + b * h)) for a, b in pts], fill=col)
    img.alpha_composite(_down(big, img.size))


def ring(img: Image.Image, cx: float, cy: float, r: float, value: float, rgb: tuple, k: float) -> None:
    """Küçük ilerleme halkası (Saat uygulamasının kalan süre halkası gibi)."""
    big = _big(img.size)
    d = ImageDraw.Draw(big)
    w = round(2.6 * k * SS)
    box = [(cx - r) * SS, (cy - r) * SS, (cx + r) * SS, (cy + r) * SS]
    d.ellipse(box, outline=(*rgb, 70), width=w)
    if value > 0:
        d.arc(box, -90, -90 + 360 * min(1.0, value), fill=(*rgb, 255), width=w)
    img.alpha_composite(_down(big, img.size))


# ------------------------------------------------------------------ içerik
@dataclass
class Entry:
    """Adada gösterilecek tek şey."""
    key: str
    kind: str              # status | card
    glyph: str
    title: str
    value: str = ""
    body: str = ""
    severity: str = "info"
    angle: float = 0.0
    progress: float | None = None
    seq: int = 0

    @property
    def rgb(self) -> tuple:
        return COLORS.get(self.severity, BLUE)


def _split(text: str) -> tuple[str, str]:
    t = tr_sentence(" ".join(str(text or "").split()))
    if " · " in t:
        a, b = t.split(" · ", 1)
        return a, b
    m = re.match(r"^(.*?)\s+(~?[\d.,]+\s*\S*)$", t)  # "Yakıt ~4 dk" -> ("Yakıt", "~4 dk")
    return (m.group(1), m.group(2)) if m and m.group(1) else (t, "")


def entries(items: list[Item]) -> list[Entry]:
    """HUD komutları -> Ada içerikleri, önem sırasıyla (kritik önce, aynı önemde yeni olan önce). Kenar ışığı ve
    odak halkası ayrıca çizilir; odak halkasının yazısı Adaya gelir."""
    out: list[Entry] = []
    for it in items:
        d, key = it.data, it.id or f"#{it.seq}"
        sev = d.get("severity", "info")
        if it.kind == "arrow":
            title, value = _split(d.get("text") or "Tehdit")
            out.append(Entry(key, "status", "arrow", title, value, severity=sev,
                             angle=(clock_of(d.get("clock")) or 12) % 12 * 30.0, seq=it.seq))
        elif it.kind == "label":
            title, value = _split(d.get("text") or "")
            g = d.get("icon") or ("drop" if re.search(r"yak[ıi]t", title, re.I) else
                                  "warning" if sev != "info" else "sparkle")
            out.append(Entry(key, "status", g, title, value, severity=sev, seq=it.seq))
        elif it.kind == "progress":
            title, value = _split(d.get("text") or "")
            g = d.get("icon") or ("thermometer" if "°" in (d.get("text") or "") else "gauge")
            out.append(Entry(key, "status", g, title, value, severity=sev, seq=it.seq,
                             progress=max(0.0, min(1.0, float(d.get("value") or 0)))))
        elif it.kind == "box" and d.get("text"):
            out.append(Entry(key, "status", "cursor", tr_sentence(str(d["text"])), severity="info", seq=it.seq))
        elif it.kind == "card":
            title = str(d.get("title") or "ALICE")
            g = d.get("icon") or ("check" if title.lower().startswith("düşürdün") else
                                  "warning" if sev in ("warn", "critical") else "sparkle")
            out.append(Entry(key, "card", g, title, body=" ".join(str(d.get("text") or "").split()), severity=sev,
                             seq=it.seq))
    out.sort(key=lambda e: (PRIORITY.get(e.severity, 2), -e.seq))
    return out


# ------------------------------------------------------------------ Ada
@dataclass
class Layout:
    w: float
    h: float
    r: float


def island_layout(e: Entry | None, k: float) -> Layout:
    """İçeriğin istediği Ada boyutu. Kompakt: 38 pt yüksek kapsül; genişletilmiş (kart): 400 pt genişlik, köşe 26."""
    if e is None:
        return Layout(126 * k, 38 * k, 19 * k)  # boşta: küçük kapsül (gösterilmez)
    if e.kind == "card":  # HIG: sadece içeriğin gerektirdiği kadar yer; en fazla 400 pt
        fb = font(round(14 * k), 400, False)
        need = 72 * k + max(_tw(e.title, font(round(16 * k), 600)), _tw(e.body, fb) if e.body else 0)
        w = min(400 * k, max(240 * k, need))
        lines = _wrap(e.body, fb, round(w - 72 * k)) if e.body else []
        h = (20 + 22 + (6 + 19 * len(lines) if lines else 0) + 18) * k
        return Layout(w, max(h, 64 * k), 26 * k)
    ft = font(round(15 * k), 600)
    w = 14 * k + 20 * k + 9 * k + _tw(e.title, ft) + (10 * k + _tw(e.value, ft) if e.value else 0) + 16 * k
    if e.progress is not None:
        w += 26 * k
    return Layout(max(w, 112 * k), 38 * k, 19 * k)


MARGIN = 48  # gölge ve ışıma payı (k cinsinden): bulanıklık kesilmesin


def _ss_mask(W: int, H: int, box: tuple, r: float) -> Image.Image:
    big = Image.new("L", (W * SS, H * SS), 0)
    ImageDraw.Draw(big).polygon([(x * SS, y * SS) for x, y in squircle(box, r)], fill=255)
    return big.resize((W, H), Image.LANCZOS)


@lru_cache(maxsize=64)
def _glass_parts(w: int, h: int, r: int, crit: bool, pulse_q: float, k: float) -> tuple:
    """Buzlu cam parçaları (boyut başına bir kez): (gölge ya da ışıma, şekil maskesi, kılcal çizgi + ışık)."""
    m = round(MARGIN * k)
    W, H = w + 2 * m, h + 2 * m
    box = (m, m, m + w, m + h)
    q = 4  # bulanıklık çeyrek çözünürlükte: aynı görünür, 16 kat ucuz
    under = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    small = Image.new("RGBA", (max(1, W // q), max(1, H // q)), (0, 0, 0, 0))
    if crit:  # kritik: nefes alan kırmızı ışıma
        g = (3 + 3 * pulse_q) * k
        poly = squircle((box[0] - g, box[1] - g, box[2] + g, box[3] + g), r + g)
        fill, blur = (*GLOW["critical"], int(80 + 70 * (1 - pulse_q))), 9 * k
    else:  # Liquid Glass gibi çok hafif gölge: açık gökyüzünde de cam seçilsin
        poly = squircle((box[0], box[1] + 3 * k, box[2], box[3] + 3 * k), r)
        fill, blur = (0, 0, 0, 46), 10 * k
    ImageDraw.Draw(small).polygon([(x / q, y / q) for x, y in poly], fill=fill)
    under.alpha_composite(small.filter(ImageFilter.GaussianBlur(blur / q)).resize((W, H), Image.BILINEAR))
    mask = _ss_mask(W, H, box, r)
    big = _big((W, H))
    d = ImageDraw.Draw(big)
    pts = [(x * SS, y * SS) for x, y in squircle(box, r)]
    d.line(pts + [pts[0]], fill=(*RED, 220) if crit else HAIRLINE,
           width=max(1, round((1.6 if crit else 1.0) * k * SS)))
    inner = [(x * SS, y * SS) for x, y in squircle((box[0] + k, box[1] + k, box[2] - k, box[3] - k),
                                                    max(0.0, r - k))]
    edge = box[1] + min(h * 0.45, r * 1.2)
    top = sorted((pt for pt in inner if pt[1] < edge * SS), key=lambda pt: pt[0])  # yalnız üst kenar
    if len(top) > 2:
        d.line(top, fill=HIGHLIGHT, width=max(1, round(1.0 * k * SS)), joint="curve")
    over = big.resize((W, H), Image.LANCZOS)
    return under, mask, over


def _glass(w: int, h: int, r: int, crit: bool, pulse_q: float, k: float, behind: Image.Image | None) -> Image.Image:
    """Bir cam yüzey: arkadaki (bulanık) ekran + açık tül, şekle kırpılmış; üstte kılcal çizgi ve ışık."""
    under, mask, over = _glass_parts(w, h, r, crit, pulse_q, k)
    img = under.copy()
    if behind is not None and behind.size == under.size:
        fill = behind.convert("RGBA")
        fill.alpha_composite(Image.new("RGBA", fill.size, TINT))
    else:
        fill = Image.new("RGBA", under.size, TINT_SOLID)
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), mask))
    img.alpha_composite(fill)
    img.alpha_composite(over)
    return img


@lru_cache(maxsize=64)
def _content(sig: tuple, w: int, h: int, k: float) -> Image.Image:
    """Adanın içeriği (simge, yazı, değer), hedef boyutta bir kez çizilir."""
    kind, gname, title, value, body, sev, angle, progress = sig
    rgb = COLORS.get(sev, BLUE)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if kind == "card":
        pad = 20 * k
        glyph(img, gname, pad + 9 * k, 31 * k, 20 * k, LABEL if gname == "sparkle" else rgb)
        d = ImageDraw.Draw(img)
        tx = pad + 30 * k
        text(d, (tx, 31 * k), title, font(round(16 * k), 600), (*LABEL, 255), "lm")
        if body:
            fb = font(round(14 * k), 400, False)
            y = 48 * k
            for ln in _wrap(body, fb, round(w - tx - pad)):
                text(d, (tx, y), ln, fb, LABEL_2, "la", tracking=-0.01)
                y += 19 * k
        return img
    cy = h / 2
    glyph(img, gname, 24 * k, cy, 18 * k, rgb, float(angle))
    d = ImageDraw.Draw(img)
    ft = font(round(15 * k), 600)
    text(d, (43 * k, cy), title, ft, (*LABEL, 255), "lm")
    right = w - 16 * k
    if progress is not None:
        ring(img, right - 8 * k, cy, 8 * k, progress, rgb, k)
        right -= 26 * k
    if value:
        text(d, (right, cy), value, ft, (*rgb, 255), "rm")
    return img


def _sig(e: Entry) -> tuple:
    return (e.kind, e.glyph, e.title, e.value, e.body, e.severity, round(e.angle),
            None if e.progress is None else round(e.progress, 2))


def _fade(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, a))))
    return out


def _behind(backdrop: Image.Image | None, x: int, y: int, W: int, H: int) -> Image.Image | None:
    """Bulanık arka planın bu yüzeyin altına düşen parçası (tuval koordinatı)."""
    if backdrop is None:
        return None
    return backdrop.crop((x, y, x + W, y + H))


def draw_island(img: Image.Image, e: Entry | None, box: tuple, r: float, k: float, content_alpha: float,
                pulse: float = 0.0, backdrop: Image.Image | None = None) -> None:
    x0, y0, x1, y1 = box
    w, h = max(1, round(x1 - x0)), max(1, round(y1 - y0))
    crit = e is not None and e.severity == "critical"
    m = round(MARGIN * k)
    px, py = round(x0) - m, round(y0) - m
    body = _glass(w, h, round(min(r, h / 2)), crit, round(pulse * 6) / 6 if crit else 0.0, k,
                  _behind(backdrop, px, py, w + 2 * m, h + 2 * m))
    img.alpha_composite(body, (px, py))
    if e is None or content_alpha <= 0.01:
        return
    lay = island_layout(e, k)
    c = _content(_sig(e), round(lay.w), round(lay.h), k)
    if c.width > w or c.height > h:  # yay henüz açılmadı: taşan kısmı gösterme
        c = c.crop((0, 0, min(c.width, w), min(c.height, h)))
    img.alpha_composite(_fade(c, content_alpha), (round(x0), round(y0)))


def draw_minimal(img: Image.Image, e: Entry, cx: float, cy: float, d_: float, k: float, alpha: float,
                 backdrop: Image.Image | None = None) -> None:
    """Ada yanında ayrık küçük cam daire: ikinci/üçüncü içeriğin yalnızca simgesi (HIG minimal görünüm)."""
    d = max(2, round(d_))
    m = round(MARGIN * k)
    px, py = round(cx - d / 2) - m, round(cy - d / 2) - m
    layer = _glass(d, d, d // 2, e.severity == "critical", 0.0, k, _behind(backdrop, px, py, d + 2 * m, d + 2 * m))
    glyph(layer, e.glyph, m + d / 2, m + d / 2, 16 * k, COLORS.get(e.severity, BLUE), e.angle)
    img.alpha_composite(_fade(layer, alpha), (px, py))


class Spring:
    """Sönümlü yay (hafif aşıp oturur): Adanın boyutu içeriğe göre böyle değişir."""

    def __init__(self, value: float, stiffness: float = 260.0, damping: float = 24.0):
        self.x, self.v, self.target = value, 0.0, value
        self.k, self.c = stiffness, damping

    def step(self, dt: float) -> bool:
        """Bir kare ilerlet; hâlâ hareket ediyorsa True."""
        dt = min(dt, 1 / 30)
        for _ in range(4):
            h = dt / 4
            a = self.k * (self.target - self.x) - self.c * self.v
            self.v += a * h
            self.x += self.v * h
        if abs(self.target - self.x) < 0.3 and abs(self.v) < 3:
            self.x, self.v = self.target, 0.0
            return False
        return True


class Island:
    """Adanın canlı durumu: hangi içerik, boyut yayları, içerik geçişi. Her karede `frame` resmi verir."""

    MAX_W, MAX_H, MINIMALS = 420, 170, 2

    def __init__(self, sw: int, sh: int, top: float = 10.0, backdrop=None):
        self.backdrop = backdrop   # () -> Adanın tuvali boyunda bulanık arka plan ya da None
        self.k = scale_for(sh)
        k = self.k
        self.sw, self.sh = sw, sh
        self.cw, self.ch = round((self.MAX_W + 2 * 60 + 2 * 46 + 2 * MARGIN) * k), round((self.MAX_H + 40 + MARGIN) * k)
        self.x, self.y = round(sw / 2 - self.cw / 2), 0
        self.top = top * k
        idle = island_layout(None, k)
        self.w, self.h, self.r = Spring(idle.w), Spring(idle.h), Spring(idle.r)
        self.shown = 0.0           # 0-1: Adanın kendisi görünür mü (boşken söner)
        self.current: Entry | None = None
        self.prev: Entry | None = None
        self.changed_at = -1e9
        self.minimals: list[Entry] = []
        self.started = -1e9
        self.last_t: float | None = None

    def update(self, ents: list[Entry], now: float) -> bool:
        """Yeni içerik listesi. Görünecek bir şey varsa True."""
        top = ents[0] if ents else None
        cur = self.current
        if top is not None and (cur is None or top.key != cur.key or (top.title, top.value, top.body, top.angle,
                                                                        top.progress) !=
                                (cur.title, cur.value, cur.body, cur.angle, cur.progress)):
            same = cur is not None and top.key == cur.key
            if not same:
                self.prev, self.changed_at = cur, now
            if cur is None:
                self.started = now
            self.current = top
        elif top is None and cur is not None:
            self.prev, self.current, self.changed_at = cur, None, now
        self.minimals = ents[1:1 + self.MINIMALS]
        lay = island_layout(self.current or self.prev, self.k)
        self.w.target, self.h.target, self.r.target = lay.w, lay.h, lay.r
        return self.current is not None or self.shown > 0.01

    def frame(self, now: float) -> tuple[Image.Image, bool]:
        """(resim, hâlâ hareket ediyor mu)."""
        dt = 1 / 60 if self.last_t is None else now - self.last_t
        self.last_t = now
        moving = any([self.w.step(dt), self.h.step(dt), self.r.step(dt)])
        target_shown = 1.0 if self.current is not None else 0.0
        if abs(self.shown - target_shown) > 0.001:
            self.shown += (target_shown - self.shown) * min(1.0, dt / (FADE_IN if target_shown else FADE_OUT) * 2.2)
            if abs(self.shown - target_shown) < 0.01:
                self.shown = target_shown
            moving = True
        t = (now - self.changed_at) / 0.32  # içerik geçişi: eski söner, yeni belirir
        content = self.current if t >= 0.4 or self.prev is None else self.prev
        content_alpha = (min(1.0, (t - 0.4) / 0.6) if t < 1 else 1.0) if content is self.current else max(0.0, 1 - t / 0.4)
        if t < 1:
            moving = True
        pulse = 0.0
        if self.current is not None and self.current.severity == "critical":
            pulse = 0.5 + 0.5 * math.sin((now - self.started) * 2 * math.pi / PULSE)
            moving = True
        img = Image.new("RGBA", (self.cw, self.ch), (0, 0, 0, 0))
        k = self.k
        bd = None
        if self.shown > 0.005 and self.backdrop is not None:
            try:
                bd = self.backdrop()
            except Exception:  # noqa: BLE001 - arka plan alınamazsa düz açık cam
                bd = None
        if self.shown > 0.005:
            cx, top = self.cw / 2, self.top
            w, h = self.w.x, self.h.x
            grow = 0.86 + 0.14 * self.shown  # belirirken küçükten
            w, h = w * grow, h * grow
            box = (cx - w / 2, top, cx + w / 2, top + h)
            draw_island(img, content, box, min(self.r.x, h / 2), k, content_alpha, pulse, bd)
            x = box[2] + 6 * k
            for e in self.minimals:  # sağda ayrık küçük daireler
                d_ = 38 * k * grow
                draw_minimal(img, e, x + d_ / 2, top + d_ / 2, d_, k, 1.0, bd)
                x += d_ + 6 * k
            if self.shown < 0.999:
                img.putalpha(img.getchannel("A").point(lambda v: int(v * self.shown)))
        return img, moving


# ------------------------------------------------------------------ kenar ışığı ve odak halkası
@dataclass
class Sprite:
    key: str
    item: Item
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0


def layout(items: list[Item], sw: int, sh: int) -> list[Sprite]:
    """Ada dışında çizilenler: kritik/uyarı tehdidin kenar ışığı ve tıklanacak yerin odak halkası."""
    k = scale_for(sh)
    out: list[Sprite] = []
    for it in items:
        key = it.id or f"#{it.seq}"
        if it.kind == "edge":
            out.append(_edge_sprite(key, it, sw, sh, k))
        elif it.kind == "box":
            d = it.data
            pad = round(16 * k)
            out.append(Sprite(key, it, round(float(d["x"])) - pad, round(float(d["y"])) - pad,
                              round(float(d["w"])) + 2 * pad, round(float(d["h"])) + 2 * pad))
    return out


def _edge_sprite(key: str, it: Item, sw: int, sh: int, k: float) -> Sprite:
    """Işının değdiği kenarda yumuşak ışık; köşeye yakınsa köşede yuvarlak ışık."""
    ang = (clock_of(it.data.get("clock")) or 12) % 12 * 30.0
    dx, dy = math.sin(math.radians(ang)), -math.cos(math.radians(ang))
    tx = (sw / 2) / abs(dx) if abs(dx) > 1e-6 else math.inf
    ty = (sh / 2) / abs(dy) if abs(dy) > 1e-6 else math.inf
    t = min(tx, ty)
    ex, ey = sw / 2 + dx * t, sh / 2 + dy * t
    depth = round(110 * k)
    if min(ex, sw - ex) < sw * 0.14 and min(ey, sh - ey) < sh * 0.14:
        r = round(320 * k)
        return Sprite(key, it, 0 if ex < sw / 2 else sw - r, 0 if ey < sh / 2 else sh - r, r, r, ang)
    if tx <= ty:
        length = round(sh * 0.5)
        return Sprite(key, it, 0 if ex < sw / 2 else sw - depth, round(min(max(0, ey - length / 2), sh - length)),
                      depth, length, ang)
    length = round(sw * 0.34)
    return Sprite(key, it, round(min(max(0, ex - length / 2), sw - length)), 0 if ey < sh / 2 else sh - depth,
                  length, depth, ang)


@lru_cache(maxsize=16)
def _edge_mask(w: int, h: int, left: bool, top: bool) -> Image.Image:
    sx, sy = max(2, w // 4), max(2, h // 4)
    mask = Image.new("L", (sx, sy), 0)
    px = mask.load()
    square = abs(w - h) < 4
    for j in range(sy):
        for i in range(sx):
            u, v = (i + 0.5) / sx, (j + 0.5) / sy
            if square:
                val = max(0.0, 1 - math.hypot(u if left else 1 - u, v if top else 1 - v)) ** 2.4
            elif w < h:
                val = max(0.0, 1 - (u if left else 1 - u)) ** 2.2 * math.exp(-((v - 0.5) ** 2) / 0.05)
            else:
                val = max(0.0, 1 - (v if top else 1 - v)) ** 2.2 * math.exp(-((u - 0.5) ** 2) / 0.05)
            px[i, j] = int(255 * val)
    return mask.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))


def render(sp: Sprite, sh: int, alpha: float = 1.0, grow: float = 1.0, pulse: float = 0.0) -> Image.Image:
    k = scale_for(sh)
    img = Image.new("RGBA", (max(1, sp.w), max(1, sp.h)), (0, 0, 0, 0))
    sev = sp.item.data.get("severity", "info")
    rgb = GLOW.get(sev, GLOW["info"]) if sp.item.kind == "edge" else BLUE  # kenar ışığı canlı, halka marka mavisi
    if sp.item.kind == "edge":
        peak = (0.5 + 0.35 * (1 - pulse)) if sev == "critical" else 0.5
        mask = _edge_mask(img.width, img.height, sp.x <= 0, sp.y <= 0).point(lambda v: int(v * peak))
        glow = Image.new("RGBA", img.size, (*rgb, 0))
        glow.putalpha(mask)
        img.alpha_composite(glow)
    elif sp.item.kind == "box":  # macOS odak halkası: mavi, eş merkezli sürekli köşe, yumuşak dış ışık
        d = sp.item.data
        pad = 16 * k
        bw, bh = float(d["w"]), float(d["h"])
        r = min(10 * k, bh / 3, bw / 3)
        g = 3 * k
        box = (pad - g, pad - g, pad + bw + g, pad + bh + g)
        halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(halo).line(squircle(box, r + g) + [squircle(box, r + g)[0]], fill=(*BLUE, 150),
                                  width=round(6 * k))
        img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(5 * k)))
        _shape(img, box, r + g, (0, 0, 0, 0), (*BLUE, 235), 3 * k)
    return adjust(img, alpha, grow)


def _with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    r, g, b, al = img.split()
    return Image.merge("RGBA", (r, g, b, al.point(lambda v: int(v * max(0.0, a)))))


def adjust(img: Image.Image, alpha: float = 1.0, grow: float = 1.0) -> Image.Image:
    if abs(grow - 1.0) > 1e-3:
        W, H = img.size
        w, h = max(1, round(W * grow)), max(1, round(H * grow))
        scaled = img.resize((w, h), Image.BILINEAR)
        out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if grow <= 1:
            out.alpha_composite(scaled, ((W - w) // 2, (H - h) // 2))
        else:
            ox, oy = (w - W) // 2, (h - H) // 2
            out.alpha_composite(scaled.crop((ox, oy, ox + W, oy + H)))
        img = out
    return _with_alpha(img, alpha)


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def spring(t: float) -> float:
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 1 - math.exp(-6.5 * t) * math.cos(7.5 * t) * (1 - t) ** 0.5


@dataclass
class Anim:
    """Kenar ışığı ve odak halkasının animasyonu."""
    shown_at: float
    gone_at: float | None = None
    angle: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def frame(self, now: float, sp: Sprite) -> tuple[float, float, float, bool]:
        t_in = (now - self.shown_at) / FADE_IN
        alpha = ease_out(t_in)
        moving = t_in < 1.5
        if self.gone_at is not None:
            t_out = (now - self.gone_at) / FADE_OUT
            alpha *= 1 - ease_out(t_out)
            moving = t_out < 1
        grow = 1.08 - 0.08 * spring(t_in / 1.5) if sp.item.kind == "box" else 1.0  # odak halkası dışarıdan oturur
        pulse = 0.0
        if sp.item.kind == "edge" and sp.item.data.get("severity") == "critical":
            pulse = 0.5 + 0.5 * math.sin((now - self.shown_at) * 2 * math.pi / PULSE)
            moving = True
        return alpha, grow, pulse, moving


def blur_backdrop(img: Image.Image, k: float = 1.0) -> Image.Image:
    """Camın arkası: ekran parçası çeyrek çözünürlükte bulanıklaştırılıp biraz doygunlaştırılır (Apple'ın cam
    malzemesi rengi soldurmaz, yumuşatır). Ucuz: 700x200'lük parça için ~2 ms."""
    from PIL import ImageEnhance

    q = 4
    small = img.convert("RGB").resize((max(1, img.width // q), max(1, img.height // q)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(18 * k / q))
    small = ImageEnhance.Color(small).enhance(1.35)
    return small.resize(img.size, Image.BILINEAR)


def preview(items: list[dict[str, Any]], sw: int = 1920, sh: int = 1080,
            background: Image.Image | None = None) -> Image.Image:
    """Komutların son halini tek resimde göster (rapor ve deneme için; pencere açmaz)."""
    from alice.hud.model import HudModel

    m = HudModel()
    for msg in items:
        m.apply(msg, 0.0)
    img = (background.convert("RGBA").resize((sw, sh)) if background is not None
           else Image.new("RGBA", (sw, sh), (0, 0, 0, 0)))
    for sp in layout(m.visible(), sw, sh):
        img.alpha_composite(render(sp, sh, pulse=0.3), (sp.x, sp.y))
    isl = Island(sw, sh)
    if background is not None:  # buzlu cam: arkadaki resmin bulanık hali
        bg = img.crop((isl.x, isl.y, isl.x + isl.cw, isl.y + isl.ch)).convert("RGB")
        blurred = blur_backdrop(bg, isl.k)
        isl.backdrop = lambda: blurred
    isl.update(entries(m.visible()), 0.0)
    for t in range(0, 120):  # yaylar ve geçişler otursun
        frame, _ = isl.frame(t / 60)
    img.alpha_composite(frame, (isl.x, isl.y))
    return img
