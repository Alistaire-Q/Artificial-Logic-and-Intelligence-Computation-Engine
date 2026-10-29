"""Söz kesilince ALICE'in cevabının ne kadarı duyuldu.

Kullanıcı ALICE konuşurken araya girince ses kesiliyor ama model cevabın tamamını söylediğini sanıyordu: "devam et"
deyince baştan anlatıyor ya da duyulmayan kısmı söylenmiş sayıp atlıyordu. Burada cevabın duyulan ve duyulmayan
kısmı ayrılır, modele kısa bir not olarak verilir.
"""

from __future__ import annotations

MIN_UNHEARD = 25  # karakter: bundan kısa kalan kısım için not gerekmez (cümlenin sonu zaten belliydi)


def split_heard(text: str, fraction: float) -> tuple[str, str]:
    """Metni çalınan ses oranına göre (0..1) duyulan/duyulmayan diye ikiye böl; kelime ortasından bölmez."""
    text = " ".join((text or "").split())
    fraction = max(0.0, min(1.0, fraction))
    cut = int(len(text) * fraction)
    if cut >= len(text):
        return text, ""
    space = text.rfind(" ", 0, cut + 1)
    cut = space if space > 0 else 0
    return text[:cut].strip(), text[cut:].strip()


def cut_note(heard: str | None, unheard: str) -> str:
    """Modele giden not ("" = gerek yok). heard None: duyulan kısım bilinmiyor (cevabın başı)."""
    if len(unheard) < MIN_UNHEARD:
        return ""
    said = "" if heard is None else f'Duyduğu kısım: "{heard[-300:]}". ' if heard else "Cevabından hiçbir şey duymadı. "
    return ("[Not: kullanıcı sözünü kesti. " + said + f'Duymadığı kısım: "{unheard[:600]}". '
            "Yeni söylediğine cevap ver; \"devam et\" derse duymadığı kısımdan sürdür, duyduğunu tekrarlama.]")
