"""HUD'un görünümü: her öğe ekranda kendi küçük, kenarı yumuşatılmış, yarı saydam resmi (sprite).

Dil macOS'in koyu HUD panelleri: yarı saydam koyu cam, ince açık kenar, yumuşak gölge, iOS sistem renkleri,
Segoe UI. Süs yok: renk sadece önem için (mavi bilgi, turuncu uyarı, kırmızı kritik). Animasyonlar kısa ve sakin:
belirirken 220 ms solma ve hafif büyüme, kaybolurken 180 ms solma, kritik öğede yavaş nefes alan bir hale, yönü
değişen ok yeni yönüne döner (zıplamaz).

Bu modül Windows'a dokunmaz: `layout` ekrandaki yerleri, `render` resmi verir. Pencereye basmak window.py'nin işi.
Şekiller 3 kat büyük çizilip küçültülür (kenar yumuşatma), yazı son boyutta çizilir (keskin kalsın).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from alice.hud.model import Item, clock_of

# iOS koyu mod sistem renkleri
COLORS = {"info": (10, 132, 255), "warn": (255, 159, 10), "critical": (255, 69, 58)}
GLASS = (28, 28, 30, 196)        # panel dolgusu
STROKE = (255, 255, 255, 34)     # panelin ince kenarı
TEXT = (255, 255, 255, 245)
TEXT_2 = (235, 235, 245, 168)    # ikincil yazı (iOS secondaryLabel)
SS = 3                           # kenar yumuşatma için büyütme

FADE_IN, FADE_OUT, TURN = 0.22, 0.18, 0.2   # sn
PULSE = 1.6                                 # sn: kritik halenin nefesi


@dataclass
class Sprite:
    key: str
    item: Item
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0   # ok: ekranda derece (0 yukarı, saat yönünde)


def scale_for(screen_h: int) -> float:
    """1080p'de 1; 1440p'de 1,33; 4K'da 2. Yazı ve şekiller ekranla birlikte büyür."""
    return max(1.0, min(2.0, screen_h / 1080))


@lru_cache(maxsize=32)
def font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    names = {"semibold": ("seguisb.ttf", "segoeuib.ttf"), "regular": ("segoeui.ttf",)}[weight]
    for name in (*names, "arial.ttf", "DejaVuSans.ttf"):
        for base in ("C:/Windows/Fonts/", ""):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
    return ImageFont.load_default()


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def color(item: Item) -> tuple[int, int, int]:
    c = item.data.get("color")
    if isinstance(c, str) and len(c) == 7 and c.startswith("#"):
        try:
            return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
        except ValueError:
            pass
    return COLORS.get(item.data.get("severity", "info"), COLORS["info"])


def angle_of(clock: int) -> float:
    return (clock % 12) * 30.0


def direction(deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return math.sin(a), -math.cos(a)


def _text_w(s: str, f: ImageFont.FreeTypeFont) -> int:
    return int(math.ceil(f.getlength(s)))


def _wrap(s: str, f: ImageFont.FreeTypeFont, width: int, max_lines: int = 4) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if cur and _text_w(t, f) > width:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and _text_w(last + "…", f) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# ------------------------------------------------------------------ yerleşim
def _arrow_size(item: Item, k: float) -> tuple[int, int, int]:
    disc = round(46 * k)
    label = str(item.data.get("text") or "")
    pill_w = _text_w(label, font(round(13 * k), "semibold")) + round(34 * k) if label else 0
    w = max(disc, pill_w) + round(28 * k)  # gölge payı
    h = disc + (round(30 * k) + round(8 * k) if label else 0) + round(28 * k)
    return w, h, disc


def _card_size(item: Item, k: float) -> tuple[int, int]:
    w = round(340 * k)
    body = " ".join(str(item.data.get("text") or "").split())
    lines = _wrap(body, font(round(13 * k)), w - round(48 * k)) if body else []
    h = round(46 * k) + len(lines) * round(19 * k) + round(14 * k)
    return w + round(32 * k), h + round(32 * k)


def layout(items: list[Item], sw: int, sh: int) -> list[Sprite]:
    """Görünür öğeler -> ekrandaki yerleri (monitörün sol üstüne göre piksel). Yerleşim sabit kurallarla:
    oklar merkezin çevresinde bir halkada, kartlar sağ üstte alt alta, göstergeler altta, yazı üst ortada."""
    k = scale_for(sh)
    cx, cy = sw / 2, sh / 2
    ring = min(sw, sh) * 0.24
    out: list[Sprite] = []
    card_y, gauge_y = round(16 * k), sh - round(72 * k)
    for it in items:
        key = it.id or f"#{it.seq}"
        if it.kind == "arrow":
            ang = angle_of(clock_of(it.data.get("clock")) or 12)
            w, h, disc = _arrow_size(it, k)
            dx, dy = direction(ang)
            px, py = cx + dx * ring, cy + dy * ring  # diskin merkezi
            pad = round(14 * k)
            out.append(Sprite(key, it, round(px - w / 2), round(py - disc / 2 - pad), w, h, ang))
        elif it.kind == "edge":
            out.append(_edge_sprite(key, it, sw, sh, k))
        elif it.kind == "card":
            w, h = _card_size(it, k)
            out.append(Sprite(key, it, sw - w - round(8 * k), card_y, w, h))
            card_y += h - round(22 * k)
        elif it.kind == "label":
            text = str(it.data.get("text") or "")
            w = _text_w(text, font(round(15 * k), "semibold")) + round(46 * k) + round(28 * k)
            h = round(38 * k) + round(28 * k)
            x = it.data.get("x")
            y = it.data.get("y")
            lx = sw / 2 if x is None else float(x)
            ly = sh * 0.1 if y is None else float(y)
            out.append(Sprite(key, it, round(lx - w / 2), round(ly - h / 2), w, h))
        elif it.kind == "box":
            d = it.data
            pad = round(24 * k)
            label = str(d.get("text") or "")
            top = round(34 * k) if label else 0
            bw, bh = float(d["w"]), float(d["h"])
            lw = _text_w(label, font(round(12 * k), "semibold")) + round(28 * k) if label else 0
            out.append(Sprite(key, it, round(float(d["x"])) - pad, round(float(d["y"])) - pad - top,
                              round(max(bw, lw)) + 2 * pad, round(bh) + 2 * pad + top))
        elif it.kind == "progress":
            w, h = round(320 * k) + round(28 * k), round(48 * k) + round(28 * k)
            out.append(Sprite(key, it, round(cx - w / 2), gauge_y - h, w, h))
            gauge_y -= h - round(18 * k)
    return out


def _edge_sprite(key: str, it: Item, sw: int, sh: int, k: float) -> Sprite:
    """Kenar parlaması: ışının değdiği kenarda yumuşak bir ışık; köşeye yakınsa köşede yuvarlak bir ışık
    (saat 4 gibi yönlerde ince bir şerit kayboluyordu)."""
    ang = angle_of(clock_of(it.data.get("clock")) or 12)
    dx, dy = direction(ang)
    tx = (sw / 2) / abs(dx) if abs(dx) > 1e-6 else math.inf
    ty = (sh / 2) / abs(dy) if abs(dy) > 1e-6 else math.inf
    t = min(tx, ty)
    ex, ey = sw / 2 + dx * t, sh / 2 + dy * t
    depth = round(90 * k)
    corner = min(ex, sw - ex) < sw * 0.14 and min(ey, sh - ey) < sh * 0.14
    if corner:
        r = round(300 * k)
        x = 0 if ex < sw / 2 else sw - r
        y = 0 if ey < sh / 2 else sh - r
        return Sprite(key, it, x, y, r, r, ang)
    if tx <= ty:  # sol/sağ kenar
        length = round(sh * 0.42)
        x = 0 if ex < sw / 2 else sw - depth
        return Sprite(key, it, x, round(min(max(0, ey - length / 2), sh - length)), depth, length, ang)
    length = round(sw * 0.3)
    y = 0 if ey < sh / 2 else sh - depth
    return Sprite(key, it, round(min(max(0, ex - length / 2), sw - length)), y, length, depth, ang)


# ------------------------------------------------------------------ çizim
def _panel(img: Image.Image, box: tuple[float, float, float, float], radius: float, k: float,
           fill: tuple[int, int, int, int] = GLASS, stroke: tuple[int, int, int, int] = STROKE,
           shadow: bool = True) -> None:
    """Gölgeli, ince kenarlı, yuvarlak köşeli cam panel (3 kat çizilip küçültülür)."""
    W, H = img.size
    if shadow:  # gölge son boyutta: bulanıklaştırma 3 kat resimde 9 kat pahalı
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle((box[0], box[1] + 4 * k, box[2], box[3] + 4 * k), radius,
                                             fill=(0, 0, 0, 110))
        img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(10 * k)))
    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    b = tuple(v * SS for v in box)
    ImageDraw.Draw(big).rounded_rectangle(b, radius * SS, fill=fill, outline=stroke, width=max(1, round(1 * k * SS)))
    img.alpha_composite(big.resize((W, H), Image.LANCZOS))


def _dot(d: ImageDraw.ImageDraw, x: float, y: float, r: float, rgb: tuple[int, int, int], a: int = 255) -> None:
    d.ellipse((x - r, y - r, x + r, y + r), fill=(*rgb, a))


def _pill(img: Image.Image, cx: float, cy: float, text: str, rgb: tuple[int, int, int], size: int, k: float) -> None:
    f = font(size, "semibold")
    tw = _text_w(text, f)
    h = round(30 * k * size / 13)
    w = tw + round(34 * k)
    box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    _panel(img, box, h / 2, k)
    d = ImageDraw.Draw(img)
    _dot(d, box[0] + 14 * k, cy, 3.5 * k, rgb)
    d.text((box[0] + 24 * k, cy), text, font=f, fill=TEXT, anchor="lm")


def _with_alpha(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    r, g, b, al = img.split()
    al = al.point(lambda v: int(v * max(0.0, a)))
    return Image.merge("RGBA", (r, g, b, al))


def render(sp: Sprite, sh: int, alpha: float = 1.0, grow: float = 1.0, pulse: float = 0.0) -> Image.Image:
    """Sprite'ın resmi. alpha: solma (0-1); grow: belirirken büyüme (1 = son boyut); pulse: kritik nefes (0-1)."""
    k = scale_for(sh)
    img = Image.new("RGBA", (max(1, sp.w), max(1, sp.h)), (0, 0, 0, 0))
    it, rgb = sp.item, color(sp.item)
    kind = it.kind
    if kind == "arrow":
        _arrow(img, sp, rgb, k, pulse)
    elif kind == "edge":
        _edge(img, sp, rgb, pulse)
    elif kind == "card":
        _card(img, it, rgb, k)
    elif kind == "label":
        _pill(img, sp.w / 2, sp.h / 2, str(it.data.get("text") or ""), rgb, round(15 * k), k)
    elif kind == "box":
        _box(img, sp, rgb, k)
    elif kind == "progress":
        _progress(img, it, rgb, k)
    return adjust(img, alpha, grow)


def adjust(img: Image.Image, alpha: float = 1.0, grow: float = 1.0) -> Image.Image:
    """Hazır resme animasyonun o anki solmasını ve büyümesini uygula (her karede yeniden çizmek yerine)."""
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


def _arrow(img: Image.Image, sp: Sprite, rgb: tuple[int, int, int], k: float, pulse: float) -> None:
    disc = round(46 * k)
    pad = round(14 * k)
    cx, cy = sp.w / 2, pad + disc / 2
    r = disc / 2
    if sp.item.data.get("severity") == "critical":  # nefes alan hale
        big = Image.new("RGBA", (img.width * SS, img.height * SS), (0, 0, 0, 0))
        hr = (r + (4 + 5 * pulse) * k) * SS
        ImageDraw.Draw(big).ellipse((cx * SS - hr, cy * SS - hr, cx * SS + hr, cy * SS + hr),
                                    outline=(*rgb, int(70 + 90 * (1 - pulse))), width=round(2 * k * SS))
        img.alpha_composite(big.resize(img.size, Image.LANCZOS))
    _panel(img, (cx - r, cy - r, cx + r, cy + r), r, k)
    # dolu yön oku (Apple'ın konum okuna benzer): eninden uzun, arkası çentikli, yönü sp.angle
    big = Image.new("RGBA", (img.width * SS, img.height * SS), (0, 0, 0, 0))
    dx, dy = direction(sp.angle)
    px, py = -dy, dx
    shape = ((0.0, -0.62), (0.30, 0.34), (0.0, 0.12), (-0.30, 0.34))  # (yana, ileri tersi) r cinsinden
    pts = [((cx + (px * a - dx * b) * r) * SS, (cy + (py * a - dy * b) * r) * SS) for a, b in shape]
    ImageDraw.Draw(big).polygon(pts, fill=(*rgb, 255))
    img.alpha_composite(big.resize(img.size, Image.LANCZOS))
    label = str(sp.item.data.get("text") or "")
    if label:
        _pill(img, cx, cy + r + 8 * k + 15 * k, label, rgb, round(13 * k), k)


@lru_cache(maxsize=16)
def _edge_mask(w: int, h: int, left: bool, top: bool) -> Image.Image:
    """Kenar ışığının şekli (en parlak 255). Boyut ve kenar aynı kaldıkça bir kez hesaplanır."""
    sx, sy = max(2, w // 4), max(2, h // 4)
    mask = Image.new("L", (sx, sy), 0)
    px = mask.load()
    square = abs(w - h) < 4
    for j in range(sy):
        for i in range(sx):
            u, v = (i + 0.5) / sx, (j + 0.5) / sy
            if square:  # köşe: köşeden uzaklık
                val = max(0.0, 1 - math.hypot(u if left else 1 - u, v if top else 1 - v)) ** 2.2
            elif w < h:  # sol/sağ kenar
                val = max(0.0, 1 - (u if left else 1 - u)) ** 2 * math.exp(-((v - 0.5) ** 2) / 0.06)
            else:  # üst/alt kenar
                val = max(0.0, 1 - (v if top else 1 - v)) ** 2 * math.exp(-((u - 0.5) ** 2) / 0.06)
            px[i, j] = int(255 * val)
    return mask.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(2))


def _edge(img: Image.Image, sp: Sprite, rgb: tuple[int, int, int], pulse: float) -> None:
    """Yumuşak kenar ışığı: kenarda en parlak, içeri ve uçlara doğru söner; kritikte yavaşça nefes alır."""
    crit = sp.item.data.get("severity") == "critical"
    peak = (0.55 + 0.35 * (1 - pulse)) if crit else 0.6
    mask = _edge_mask(img.width, img.height, sp.x <= 0, sp.y <= 0).point(lambda v: int(v * peak))
    glow = Image.new("RGBA", img.size, (*rgb, 0))
    glow.putalpha(mask)
    img.alpha_composite(glow)


def _card(img: Image.Image, it: Item, rgb: tuple[int, int, int], k: float) -> None:
    pad = round(16 * k)
    box = (pad, pad, img.width - pad, img.height - pad)
    _panel(img, box, 14 * k, k)
    d = ImageDraw.Draw(img)
    x0 = box[0] + 16 * k
    _dot(d, x0 + 4 * k, box[1] + 25 * k, 4 * k, rgb)
    title = str(it.data.get("title") or "")
    d.text((x0 + 16 * k, box[1] + 25 * k), title, font=font(round(14 * k), "semibold"), fill=TEXT, anchor="lm")
    body = " ".join(str(it.data.get("text") or "").split())
    if body:
        f = font(round(13 * k))
        y = box[1] + 46 * k
        for ln in _wrap(body, f, round(box[2] - box[0] - 32 * k)):
            d.text((x0, y), ln, font=f, fill=TEXT_2, anchor="la")
            y += 19 * k


def _box(img: Image.Image, sp: Sprite, rgb: tuple[int, int, int], k: float) -> None:
    d = sp.item.data
    pad = round(24 * k)
    label = str(d.get("text") or "")
    top = round(34 * k) if label else 0
    x0, y0 = pad, pad + top
    x1, y1 = x0 + float(d["w"]), y0 + float(d["h"])
    big = Image.new("RGBA", (img.width * SS, img.height * SS), (0, 0, 0, 0))
    glow = Image.new("RGBA", big.size, (0, 0, 0, 0))
    rad = min(10 * k, (x1 - x0) / 3, (y1 - y0) / 3) * SS
    b = (x0 * SS, y0 * SS, x1 * SS, y1 * SS)
    ImageDraw.Draw(glow).rounded_rectangle(b, rad, outline=(*rgb, 150), width=round(6 * k * SS))
    big = Image.alpha_composite(big, glow.filter(ImageFilter.GaussianBlur(6 * k * SS)))
    ImageDraw.Draw(big).rounded_rectangle(b, rad, outline=(*rgb, 255), width=round(2 * k * SS))
    img.alpha_composite(big.resize(img.size, Image.LANCZOS))
    if label:
        f = font(round(12 * k), "semibold")
        w = _text_w(label, f) + round(22 * k)
        h = round(24 * k)
        bx = (x0, y0 - h - 8 * k, x0 + w, y0 - 8 * k)
        _panel(img, bx, h / 2, k)
        ImageDraw.Draw(img).text((bx[0] + 11 * k, (bx[1] + bx[3]) / 2), label, font=f, fill=TEXT, anchor="lm")


def _progress(img: Image.Image, it: Item, rgb: tuple[int, int, int], k: float) -> None:
    pad = round(14 * k)
    box = (pad, pad, img.width - pad, img.height - pad)
    h = box[3] - box[1]
    _panel(img, box, h / 2, k)
    d = ImageDraw.Draw(img)
    label = str(it.data.get("text") or "")
    x0, x1 = box[0] + 18 * k, box[2] - 18 * k
    d.text((x0, box[1] + h * 0.36), label, font=font(round(12 * k), "semibold"), fill=TEXT, anchor="lm")
    value = min(1.0, max(0.0, float(it.data.get("value") or 0)))
    by = box[1] + h * 0.70
    bh = 4 * k
    big = Image.new("RGBA", (img.width * SS, img.height * SS), (0, 0, 0, 0))
    bd = ImageDraw.Draw(big)
    bd.rounded_rectangle((x0 * SS, (by - bh / 2) * SS, x1 * SS, (by + bh / 2) * SS), bh / 2 * SS,
                         fill=(255, 255, 255, 40))
    if value > 0:
        xe = x0 + (x1 - x0) * value
        bd.rounded_rectangle((x0 * SS, (by - bh / 2) * SS, max(xe, x0 + bh) * SS, (by + bh / 2) * SS), bh / 2 * SS,
                             fill=(*rgb, 255))
    img.alpha_composite(big.resize(img.size, Image.LANCZOS))


# ------------------------------------------------------------------ animasyon
def spring(t: float) -> float:
    """Hafifçe aşıp oturan yay (Dynamic Island gibi): t=0'da 0, t>=1'de 1, arada en fazla ~%4 aşma."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    return 1 - math.exp(-6.5 * t) * math.cos(7.5 * t) * (1 - t) ** 0.5


@dataclass
class Anim:
    """Bir sprite'ın animasyon durumu (window.py her kare için okur)."""
    shown_at: float
    gone_at: float | None = None
    angle: float | None = None       # şu an çizilen açı (ok döner, zıplamaz)
    angle_from: float = 0.0
    angle_to: float = 0.0
    turn_at: float = -1e9

    def frame(self, now: float, sp: Sprite) -> tuple[float, float, float, bool]:
        """(alpha, grow, pulse, hâlâ hareket ediyor mu)."""
        t_in = (now - self.shown_at) / FADE_IN
        alpha = ease_out(t_in)
        moving = t_in < 1.5  # yay kısa bir süre daha oturur
        if self.gone_at is not None:
            t_out = (now - self.gone_at) / FADE_OUT
            alpha *= 1 - ease_out(t_out)
            moving = t_out < 1
        grow = 1.0
        if sp.item.kind in ("arrow", "label"):      # küçükten yerine oturur
            grow = 0.9 + 0.1 * spring(t_in / 1.5)
        elif sp.item.kind == "box":                 # odak halkası gibi dışarıdan içeri
            grow = 1.1 - 0.1 * spring(t_in / 1.5)
        pulse = 0.0
        if sp.item.data.get("severity") == "critical" and sp.item.kind in ("arrow", "edge"):
            pulse = 0.5 + 0.5 * math.sin((now - self.shown_at) * 2 * math.pi / PULSE)
            moving = True
        if sp.item.kind == "arrow":
            if self.angle is None:
                self.angle = self.angle_from = self.angle_to = sp.angle
            elif abs(((sp.angle - self.angle_to) + 180) % 360 - 180) > 0.5:
                self.angle_from, self.angle_to, self.turn_at = self.angle, sp.angle, now
            tt = (now - self.turn_at) / TURN
            diff = ((self.angle_to - self.angle_from) + 180) % 360 - 180
            self.angle = (self.angle_from + diff * ease_out(tt)) % 360
            moving = moving or tt < 1
        return alpha, grow, pulse, moving


def preview(items: list[dict[str, Any]], sw: int = 1920, sh: int = 1080,
            background: Image.Image | None = None) -> Image.Image:
    """Komut listesinin son halini tek resimde göster (rapor ve deneme için; pencere açmaz)."""
    from alice.hud.model import HudModel

    m = HudModel()
    for msg in items:
        m.apply(msg, 0.0)
    img = (background.convert("RGBA").resize((sw, sh)) if background is not None
           else Image.new("RGBA", (sw, sh), (0, 0, 0, 0)))
    for sp in layout(m.visible(), sw, sh):
        img.alpha_composite(render(sp, sh, pulse=0.3), (sp.x, sp.y))
    return img
