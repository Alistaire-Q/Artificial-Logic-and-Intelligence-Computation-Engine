"""Düşünme (reasoning) ayarı: her sağlayıcının kendi parametresine çevrilir.

Düşünen modeller (GLM, Qwen3, gpt-oss, Gemini 3, Nemotron...) basit işlerde bile dakikalarca düşünebiliyor.
Rol başına seviye: off | low | medium | high ("default" = modele bırak). config.toml:

    [thinking]
    default = "low"
    brain = "low"
    fast = "off"

Sağlayıcı parametreyi tanımazsa router isteği bir kez ayarsız tekrarlar ve o modele bir daha göndermez.
"""

from __future__ import annotations

from typing import Any

LEVELS = ("off", "low", "medium", "high", "default")
DEFAULTS = {"default": "low", "fast": "off", "voice": "off", "live-think": "medium"}


def level_for(config_data: dict[str, Any], role: str) -> str:
    t = config_data.get("thinking", {}) or {}
    lvl = str(t.get(role) or t.get("default") or DEFAULTS.get(role) or DEFAULTS["default"]).lower()
    return lvl if lvl in LEVELS else "default"


def _on(level: str) -> bool:
    # Sadece açık/kapalı destekleyen modellerde "low" da kapalı: amaç kısa cevap.
    return level in ("medium", "high")


def params(provider: str, model: str, level: str) -> dict[str, Any]:
    """Seviye -> istek gövdesine eklenecek alanlar. Bilinmeyen sağlayıcı/model için boş."""
    if level == "default" or not level:
        return {}
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
    if provider == "chatgpt":
        # ChatGPT girişi (Responses API, reasoning.effort). gpt-6-luna: none/low/medium/high; "minimal"i 400 ile
        # reddediyor ve hata "not_found" sayılıp model oturum boyunca ölü işaretleniyor, o yüzden off -> none.
        return {"reasoning_effort": "none" if level == "off" else level}
    return {}
