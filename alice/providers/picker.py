"""Akıllı model seçimi: işin alanına göre, kullanıcının erişebildiği modellerden en uygun sıra.

Her model ailesi her işte aynı iyi değil: kimi arayüz ve tasarımda, kimi kodda, kimi inceleme ve akıl
yürütmede, kimi Türkçe metinde öne çıkıyor. Burada:

1. İşin alanı görev metninden çıkarılır (tasarım, kod, inceleme, metin, basit iş, görsel).
2. Aday havuzu: kullanıcının yapılandırdığı ve ölçümde çalışan modeller (anahtarı olmayan, kalkmış ya da
   soğumadaki modeller atlanır).
3. Puan = genel kalite (ranking.QUALITY) + o ailenin o alandaki gücü; ajan işi için bağlamı dar modeller geri
   itilir. Basit işte hız önce gelir.

"auto" model seçen proje ajanı, yönetici ve kurul üyeleri bu sırayı kullanır; elle seçilen modele dokunulmaz.
"""

from __future__ import annotations

import re
from typing import Any

from alice.providers import ranking

DOMAINS = ("design", "code", "review", "writing", "light", "vision")
DOMAIN_TR = {"design": "tasarım", "code": "kod", "review": "inceleme", "writing": "metin", "light": "basit iş",
             "vision": "görsel"}

# İşin alanını görev metninden çıkaran kalıplar (ilk eşleşen değil, en çok eşleşen kazanır).
KEYWORDS: dict[str, str] = {
    "design": r"tasarım|arayüz|\bui\b|\bux\b|frontend|ön ?yüz|css|html|sayfa|site|landing|görsel kimlik|tipografi|"
              r"animasyon|responsive|mobil görünüm|tema|renk|layout|düzen|bileşen|component|figma|stil",
    "code": r"backend|api|sunucu|server|veritabanı|database|algoritma|fonksiyon|sınıf|refactor|yeniden düzenle|"
            r"hata ayıkla|debug|bug|performans|python|typescript|javascript|\.py\b|\.ts\b|\.js\b|modül|entegrasyon|"
            r"script|test yaz|birim test|unit test|cli|endpoint|şema|migration|servis|katman|uygula|implement",
    "review": r"incele|denetle|gözden geçir|review|audit|qa\b|kalite kontrol|doğrula|analiz|mimari|plan|risk|"
              r"güvenlik|security|kök neden",
    "writing": r"readme|doküman|dokümantasyon|metin|içerik|yazı|makale|blog|çeviri|açıklama yaz|kullanım kılavuzu|"
               r"copy|slogan|e-?posta|rapor yaz",
    "light": r"yeniden adlandır|rename|biçimlendir|format|dosyaları taşı|kopyala|küçük düzeltme|yazım hatası|typo|sil\b|"
             r"basit|mekanik|toplu değişiklik",
}

# Aile -> alan bonusu. İlk eşleşen kalıp kazanır; özel kalıplar önce. Puanlar göreli (genel kaliteye eklenir).
# Kaynak: genel kullanım deneyimi ve herkese açık değerlendirmeler (2026); yeni aileler çıktıkça güncellenir.
STRENGTHS: list[tuple[str, dict[str, int]]] = [
    ("claude", {"design": 14, "code": 12, "writing": 12, "review": 9, "vision": 4}),
    ("gpt-5", {"review": 11, "code": 10, "writing": 7, "design": 8, "vision": 6}),
    (r"\bo[34]\b|o3-|o4-", {"review": 12, "code": 8}),
    ("gpt-4.1", {"code": 6, "writing": 6, "design": 4, "vision": 4}),
    ("grok", {"review": 7, "code": 6, "writing": 4}),
    ("gemini-3.1-pro|gemini-pro|gemini-2.5-pro", {"design": 9, "review": 8, "code": 7, "writing": 8, "vision": 12}),
    ("flash-lite", {"light": 6, "vision": 4}),
    ("gemini", {"vision": 10, "writing": 5, "design": 4, "light": 4}),
    ("kimi", {"code": 7, "design": 6, "writing": 4}),
    ("glm", {"code": 7, "design": 5, "review": 3}),
    ("deepseek-r", {"review": 9, "code": 4}),
    ("deepseek", {"code": 8, "review": 5}),
    ("qwen3-coder|devstral|north-mini-code|codestral", {"code": 9, "design": -3, "writing": -5}),
    ("qwen", {"code": 4, "light": 3}),
    ("gpt-oss", {"review": 4, "code": 3, "light": 3}),
    ("nemotron", {"review": 3, "code": 2}),
    ("mistral-large", {"writing": 4}),
    ("llama|gemma|mistral-small|laguna", {"light": 4, "design": -4, "review": -3}),
]

# Ölçüm tablosunda (ücretsiz modeller) olmayan, kullanıcının kendi anahtarıyla eklediği ücretli aileler.
PAID_QUALITY: list[tuple[str, int]] = [
    ("claude-opus", 99), ("claude-sonnet", 96), ("claude-haiku", 84), ("claude", 92),
    ("gpt-5-nano", 78), ("gpt-5-mini", 88), ("gpt-5", 97), (r"\bo[34]-mini", 88), (r"\bo[34]\b|o3-|o4-", 95),
    ("gpt-4.1-mini", 80), ("gpt-4.1", 86), ("gpt-4o", 82), ("grok-4", 92), ("grok", 85), ("mistral-medium", 72),
]

MIN_AGENT_CONTEXT = 32_000  # bundan dar bağlamlı model ajan işinde çabuk tıkanır


def classify(text: str) -> str:
    """Görev metninden işin alanı. Eşleşme yoksa kod (ajan işlerinin çoğu kod)."""
    low = (text or "").lower()
    hits = {d: len(re.findall(rx, low)) for d, rx in KEYWORDS.items()}
    best = max(hits, key=lambda d: hits[d])
    if not hits[best]:
        return "code"
    if best == "light" and hits["light"] < 2 and (hits["code"] or hits["design"]):
        return "design" if hits["design"] >= hits["code"] else "code"
    return best


def base_quality(ref: str) -> int:
    low = ref.lower()
    for pat, q in PAID_QUALITY:
        if re.search(pat, low):
            return q
    return 0


def bonus(ref: str, domain: str) -> int:
    low = ref.lower()
    for pat, table in STRENGTHS:
        if re.search(pat, low):
            return table.get(domain, 0)
    return 0


def pool(router) -> list[str]:
    """Kullanıcının erişebildiği, şu an kullanılabilir modeller (araç destekli)."""
    refs: list[str] = []
    for role in ("brain", "fast", "vision"):
        try:
            refs += router.chain(role)
        except KeyError:
            continue
    res = (getattr(router, "auto_cache", None) or {}).get("results") or {}
    refs += [r for r, v in res.items() if isinstance(v, dict) and (v.get("ok") or v.get("ever_ok"))]
    out: list[str] = []
    for r in refs:
        if r in out or r in ranking.RANDOM_ROUTERS or r in getattr(router, "no_tools", set()) \
                or any(x in r.lower() for x in ranking.EXCLUDE):
            continue
        try:
            if router.available(r)[0]:
                out.append(r)
        except Exception:  # noqa: BLE001 - tanımsız sağlayıcı vb.
            continue
    return out


def score(router, ref: str, domain: str) -> float:
    q = ranking.quality(ref) or base_quality(ref) or 60  # bilinmeyen ama kullanıcının seçtiği model: orta
    s = q + bonus(ref, domain)
    try:
        ctx = router.context_limit(ref)
    except Exception:  # noqa: BLE001
        ctx = MIN_AGENT_CONTEXT
    if domain != "light" and ctx < MIN_AGENT_CONTEXT:
        s -= 25
    if domain == "light":  # basit işte hız önemli: ölçülen gecikme puanı düşürür
        res = ((getattr(router, "auto_cache", None) or {}).get("results") or {}).get(ref) or {}
        s -= min(20.0, float(res.get("latency") or 0) * 3)
    return s


def chain_for(router, domain: str, fallback: str = "brain") -> list[str]:
    """Alan için model sırası: en uygun önce; sonunda (havuzda olmayanlar dahil) yedek rolün zinciri."""
    if domain == "vision":
        vis = [r for r in pool(router) if _vision(router, r)]
        ranked = sorted(vis, key=lambda r: -score(router, r, domain))
    else:
        ranked = sorted(pool(router), key=lambda r: -score(router, r, domain))
    try:
        rest = router.chain(fallback)
    except KeyError:
        rest = []
    return list(dict.fromkeys(ranked + rest))


def _vision(router, ref: str) -> bool:
    p = ref.split(":", 1)[0]
    pc = getattr(router, "providers_cfg", {}).get(p)
    try:
        return bool(pc and pc.supports_vision(ref.split(":", 1)[1])) and ref not in getattr(router, "no_vision", set())
    except Exception:  # noqa: BLE001
        return False


def pick(router, text: str, fallback: str = "brain") -> tuple[str, list[str]]:
    """Görev metnine göre (alan, model sırası)."""
    domain = classify(text)
    return domain, chain_for(router, domain, fallback="fast" if domain == "light" else fallback)


def explain(ref: str, domain: str) -> str:
    """Arayüz için kısa açıklama: 'tasarım · glm-5.3'."""
    return f"{DOMAIN_TR.get(domain, domain)} · {ref.split(':', 1)[-1].split('/')[-1]}"


def table(router) -> list[dict[str, Any]]:
    """Ayarlar/ekran için: her alanda ilk üç model."""
    return [{"domain": d, "label": DOMAIN_TR[d], "models": chain_for(router, d)[:3]} for d in DOMAINS]
