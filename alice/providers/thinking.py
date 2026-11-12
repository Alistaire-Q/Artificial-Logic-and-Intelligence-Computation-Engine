"""Düşünme (reasoning) ayarı: her sağlayıcının kendi parametresine çevrilir.

Düşünen modeller (GLM, Qwen3, gpt-oss, Gemini 3, Nemotron...) basit işlerde bile dakikalarca düşünebiliyor.
Rol başına seviye: off | low | medium | high | xhigh | max ("default" = modele bırak). xhigh ve max sadece ChatGPT
girişinde var (Extra High, Max); diğer sağlayıcılarda high gider. config.toml:

    [thinking]
    default = "low"
    brain = "low"
    fast = "off"

Sağlayıcı parametreyi tanımazsa router isteği bir kez ayarsız tekrarlar ve o modele bir daha göndermez.
"""

from __future__ import annotations

from typing import Any

LEVELS = ("off", "low", "medium", "high", "xhigh", "max", "default")
DEFAULTS = {"default": "low", "fast": "off", "voice": "off", "live-think": "medium"}


def level_for(config_data: dict[str, Any], role: str) -> str:
    t = config_data.get("thinking", {}) or {}
    lvl = str(t.get(role) or t.get("default") or DEFAULTS.get(role) or DEFAULTS["default"]).lower()
    return lvl if lvl in LEVELS else "default"


BINARY = "binary"  # yalnız açık/kapalı: arayüz "kapalı / açık" diye gösterir (off ve high)


def levels_for(provider: str, model: str) -> tuple[list[str], str]:
    """Bu modelin gerçekten desteklediği düşünme seviyeleri ve türü ("graded" | "binary" | "none"). Arayüz
    yalnız bunları gösterir: önceden her modelde yedi seviyenin hepsi görünüyor, çoğu sessizce başka bir şeye
    çevriliyordu (ör. Gemini'de xhigh = high). "default" (modele bırak) her zaman var."""
    m = model.lower()
    if provider == "chatgpt" or (provider == "cliproxy" and m.startswith(("gpt-", "codex", "o3", "o4"))):
        lv = ["off", "low", "medium", "high", "xhigh"] + (["max"] if "gpt-6" in m else [])
        return lv + ["default"], "graded"
    if provider == "anthropic" or (provider == "cliproxy" and m.startswith("claude-")):
        lv = ["low", "medium", "high", "max"]  # Claude düşünmeyi tamamen kapatmaz: en az "low"
        if "haiku" not in m:
            lv.insert(3, "xhigh")
        return (["off"] if provider == "cliproxy" else []) + lv + ["default"], "graded"
    if "gpt-oss" in m:
        return ["low", "medium", "high", "default"], "graded"
    if provider == "gemini" or (provider == "cliproxy" and m.startswith("gemini-")):
        if "flash-lite" in m:
            return ["default"], "none"  # isteği 400 ile reddediyor (11 Kasım günlüğü)
        return ["off", "low", "medium", "high", "default"], "graded"
    if provider in ("openrouter", "ollama"):
        return ["off", "low", "medium", "high", "default"], "graded"
    if provider == "groq" and "qwen" in m:
        return ["off", "high", "default"], BINARY
    if provider in ("nvidia", "sambanova", "cerebras") and any(
            k in m for k in ("glm", "qwen3", "deepseek", "nemotron", "kimi", "magistral")):
        return ["off", "high", "default"], BINARY
    if provider == "cliproxy" and m.startswith("kimi-"):
        return ["off", "high", "default"], BINARY
    return ["default"], "none"


def fit(level: str, levels: list[str]) -> str:
    """Seçili seviye bu modelde yoksa en yakın desteklenen seviye (model değişince ayar sessizce boşa gitmesin)."""
    if level in levels:
        return level
    order = ["off", "low", "medium", "high", "xhigh", "max"]
    if level not in order:
        return "default"
    i = order.index(level)
    for d in range(1, len(order)):
        for j in (i - d, i + d):  # önce bir altı (daha hızlı), sonra bir üstü
            if 0 <= j < len(order) and order[j] in levels:
                return order[j]
    return "default"


def _on(level: str) -> bool:
    # Sadece açık/kapalı destekleyen modellerde "low" da kapalı: amaç kısa cevap.
    return level in ("medium", "high")


def params(provider: str, model: str, level: str) -> dict[str, Any]:
    """Seviye -> istek gövdesine eklenecek alanlar. Bilinmeyen sağlayıcı/model için boş."""
    if level == "default" or not level:
        return {}
    if provider == "chatgpt":
        # ChatGPT girişi (Responses API, reasoning.effort). gpt-6-luna: none/low/medium/high/xhigh/max; "minimal"i
        # 400 ile reddediyor ve hata "not_found" sayılıp model oturum boyunca ölü işaretleniyor, o yüzden off -> none.
        return {"reasoning_effort": "none" if level == "off" else level}
    if provider == "anthropic":  # Claude API: output_config.effort (low..max); kapatılamaz, "off" en az düşünme
        return {"reasoning_effort": "low" if level == "off" else level}
    if provider == "cliproxy":
        # Yerel CLIProxyAPI (OpenAI uyumlu uç): reasoning_effort'u Claude'un uyarlamalı düşünmesine ve Codex'in
        # reasoning.effort'una kendisi çevirir. "none" ikisinde de kapanış/az düşünme; xhigh/max olduğu gibi gider.
        return {"reasoning_effort": "none" if level == "off" else level}
    if level in ("xhigh", "max"):
        level = "high"  # diğer sağlayıcılarda en yüksek seviye
    m = model.lower()
    effort = "low" if level == "off" else level
    if "gpt-oss" in m:  # OpenAI açık ağırlıklı modeller: kapatılamaz, en az "low"
        return {"reasoning_effort": effort}
    if provider == "gemini":
        return {"reasoning_effort": "none" if level == "off" else level}
    if provider == "openrouter":
        return {"reasoning": {"enabled": False}} if level == "off" else {"reasoning": {"effort": level}}
    if provider == "groq":
        if "qwen" in m:
            return {"reasoning_effort": "default" if _on(level) else "none"}
        return {}
    if provider in ("nvidia", "sambanova", "cerebras"):
        if any(k in m for k in ("glm", "qwen3", "deepseek", "nemotron", "kimi", "magistral")):
            on = _on(level)
            return {"chat_template_kwargs": {"enable_thinking": on, "thinking": on}}
        return {}
    if provider == "ollama":
        return {"reasoning_effort": "none" if level == "off" else level}
    return {}
