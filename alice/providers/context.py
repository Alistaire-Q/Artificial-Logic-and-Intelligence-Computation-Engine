"""Modelin bağlam penceresi (context window): kaç token'lık konuşmayı kabul ettiği.

Sağlayıcının `max_input_tokens` ayarı bilinmeyen modeller için varsayılandır ve dakikalık token sınırı dar olan
ücretsiz katmanlarda (ör. Groq ~8K) üst sınırdır. Bilinen modelde pencere bu tablodan gelir; böylece GLM 5.3 ya
da Gemini gibi 1M'lik modeller 48K'da özetlenmez. Model bir isteği "çok büyük" diye reddederse router o modelin
sınırını ayrıca öğrenir (Router.size_limit).
"""

from __future__ import annotations

# Model kimliğinde geçen ilk kalıp kazanır; özel kalıplar önce.
WINDOWS: list[tuple[str, int]] = [
    ("gemini", 1_048_576), ("glm-5", 1_048_576), ("glm-4.6", 200_000), ("glm-4.5", 128_000),
    ("kimi-k2", 262_144), ("qwen3-coder", 262_144), ("qwen3", 131_072), ("deepseek", 131_072),
    ("gpt-oss", 131_072), ("llama-4", 131_072), ("llama-3.3", 131_072), ("nemotron", 131_072),
    ("mistral-large", 131_072), ("mistral-medium", 131_072), ("mistral-small", 131_072), ("devstral", 131_072),
    ("gemma-4", 131_072), ("gemma-3", 131_072),
]
# Sağlayıcı sınırı bundan küçükse model penceresinden bağımsız olarak geçerlidir (dakikalık token sınırı dar).
TPM_LIMITED_BELOW = 64_000


def model_window(model: str) -> int | None:
    low = model.lower()
    for pat, n in WINDOWS:
        if pat in low:
            return n
    return None


def context_limit(provider_max: int, model: str, learned: int | None = None) -> int:
    """Bu modele gönderilebilecek en büyük istek (token)."""
    win = model_window(model)
    if win is None:
        limit = provider_max
    elif provider_max < TPM_LIMITED_BELOW:
        limit = min(win, provider_max)
    else:
        limit = win
    if learned:
        limit = min(limit, learned)
    return limit
