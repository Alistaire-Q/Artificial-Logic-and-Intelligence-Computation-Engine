"""Config yükleme: ~/.alice/config.toml + .env dosyaları + ortam değişkenleri.

Varsayılanlar DEFAULT_CONFIG_TOML içinde durur; `alice init` aynı metni diske yazar,
böylece kullanıcının düzenlediği dosya ile koddaki varsayılanlar hiç ayrışmaz.
"""

from __future__ import annotations

import copy
import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def alice_home() -> Path:
    return Path(os.environ.get("ALICE_HOME", Path.home() / ".alice"))


DEFAULT_CONFIG_TOML = r'''
# ALICE config. Bu dosya `alice init` ile oluşturulur, istediğin gibi düzenle.
# Model referansı formatı: "provider:model_id"  (model_id içinde ":" olabilir, ilk ":" ayırır)

[agent]
# ask  = okuma dışındaki her şeyi sorar
# auto = tehlikeli komutlar ve sistem klasörlerine yazma dışında her şeye izin verir
# yolo = hiçbir şey sormaz, tam erişim
permission_mode = "auto"
max_steps = 80
# Free tier'larda TPM limiti dar, context'i bu sınırda özetleyip küçültüyoruz.
compact_at_tokens = 48000
tool_output_chars = 12000
# Geçmişte tutulacak en fazla ekran görüntüsü (eski görüntüler metne çevrilir).
keep_images = 2
# Ekran görüntüsünün modele gönderilirken küçültüleceği en fazla genişlik.
screenshot_max_width = 1366
# Tarayıcı: false = görünür pencere. browser_executable boşsa sırayla Chrome, Edge,
# Playwright Chromium denenir.
browser_headless = false
browser_executable = ""

# Arka plan çekirdeği (`alice core run`): veri toplar, kuralları değerlendirir, görevleri çalıştırır.
[core]
# Kaç saniyede bir veri toplanır.
interval = 15
# Veriler kaç gün saklanır.
retention_days = 30
# Bu saatlerde sessiz mod: sadece uyarı ve üstü, sesli konuşma yok. Boş = kapalı.
quiet_hours = "00:30-08:00"
desktop_notifications = true
# Zamanlanmış görevler ve araştırma kurallarındaki gözetimsiz ALICE'in izin modu.
# Onay gerektiren adımlar (auto'da tehlikeli komutlar) gözetimsizken reddedilir.
task_permission_mode = "auto"

# Mod program listeleri (küçük harf, süreç adı). Politika alanları da ezilebilir:
# min_severity (info|warn|critical), topics, voice, local_gpu.
[core.modes.flight]
programs = ["dcs.exe", "dcs_server.exe", "aces.exe"]
[core.modes.dev]
programs = ["unity.exe", "unity hub.exe", "code.exe", "devenv.exe", "rider64.exe"]

# ALICE'in sesi ve kulağı (`alice voice`). Ses seçimi: `alice voice audition`.
[voice]
# true: çekirdek (alice core run) sesli asistanı da başlatır, bildirimleri seslendirir.
enabled = false
# Bulut sesi "gemini:<ses>"; yerel motor "xtts" ya da "chatterbox" (boş = yok).
tts_cloud = "gemini:Aoede"
tts_local = ""
# cloud: önce bulut, local: önce yerel. Uçuşta (local_gpu = false) her zaman önce bulut.
prefer = "cloud"
gemini_tts_model = "gemini-2.5-flash-preview-tts"
# Konuşma tanıma zinciri: groq:<model>, local:<faster-whisper modeli>
stt = ["groq:whisper-large-v3-turbo", "local:large-v3-turbo"]
stt_cpu_model = "small"
language = "tr"
# Bas-konuş tuşu (pynput adı: <scroll_lock>, <f13>, <pause>...). Boş = kapalı.
ptt_key = "<scroll_lock>"
# openWakeWord modeli (.onnx). Boşsa "Hey Alice" Whisper ile, konuşma algılanınca anlaşılır.
wake_model = ""
wake_phrase = true
# ALICE konuşurken konuşmaya başlarsan susar.
barge_in = true
# ALICE cevap verdikten sonra bu kadar saniye "Hey Alice" demeden devam edebilirsin.
follow_up = 8

# Hızlı bar: her programın üstünde kısayolla açılan küçük ALICE kutusu (pynput gerekir).
[quick]
# true: çekirdek (alice core run) hızlı barı da başlatır.
enabled = true
hotkey = "<ctrl>+<alt>+a"
region_hotkey = "<ctrl>+<alt>+s"
clipboard_hotkey = "<ctrl>+<alt>+c"

# Rol başına model zinciri. Soldan sağa denenir; 429, quota, 5xx, model yok gibi
# durumlarda bir sonrakine geçilir. `alice models --refresh` bu listeleri
# canlı ücretsiz model kataloğuna göre güncellemene yardım eder.
[roles]
# Ana beyin (System 2): planlama, kod yazma, araç kullanma.
brain = [
    "gemini:gemini-flash-latest",
    "openrouter:openai/gpt-oss-120b:free",
    "nvidia:qwen/qwen3-coder-480b-a35b-instruct",
    "groq:openai/gpt-oss-120b",
    "cerebras:gpt-oss-120b",
    "openrouter:openrouter/free",
    "ollama:qwen3:8b",
]
# Hızlı, kısa işler: özet, input'a yazılacak metni üretme, decider emülasyonu.
fast = [
    "groq:openai/gpt-oss-20b",
    "cerebras:gpt-oss-120b",
    "gemini:gemini-flash-lite-latest",
    "openrouter:openrouter/free",
    "ollama:qwen3:8b",
]
# Ekran görüntüsü anlama. Beyin modeli görsel destekliyorsa direkt ona gider,
# desteklemiyorsa görüntü önce bu rolle metne çevrilir.
vision = [
    "gemini:gemini-flash-latest",
    "gemini:gemini-flash-lite-latest",
    "nvidia:meta/llama-4-maverick-17b-128e-instruct",
]

# System One (hızlı karar) motoru, soldan sağa denenir:
#   "jev"            = TypeSafe Jev API (TYPESAFE_API_KEY)
#   "jev-openrouter" = aynı Jev, OpenRouter üzerinden (OPENROUTER_API_KEY + hesapta kredi gerekir,
#                      kredi yoksa 402 alır ve 1 saat atlanır)
#   "llm:<rol>"      = o rolün ücretsiz modelleriyle Jev formatını taklit eden yedek
[decider]
chain = ["jev", "jev-openrouter", "llm:fast"]
jev_url = "https://api.typesafe.ai/v1/systemone"
jev_model = "jev-latest"
jev_api_key_env = "TYPESAFE_API_KEY"
openrouter_jev_url = "https://openrouter.ai/api/v1/systemone"
openrouter_jev_model = "~typesafe/jev-latest"
openrouter_jev_api_key_env = "OPENROUTER_API_KEY"
# Bu güvenin altındaki kararlar reflex döngüsünde beyne (System 2) devredilir.
min_confidence = 0.55

[providers.gemini]
base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
api_key_env = "GEMINI_API_KEY"
vision_models = ["gemini-*"]
max_input_tokens = 1000000

[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
vision_models = ["google/gemma-*", "meta-llama/llama-4-*", "*vl*", "*vision*"]
max_input_tokens = 128000
headers = { "HTTP-Referer" = "https://github.com/hayatexe/Artificial-Logic-and-Intelligence-Computation-Engine", "X-Title" = "ALICE" }

[providers.groq]
base_url = "https://api.groq.com/openai/v1"
api_key_env = "GROQ_API_KEY"
vision_models = ["meta-llama/llama-4-*"]
# Groq free tier ~8K TPM: büyük context'li istekler burada 413/429 alır,
# router bu yüzden bu sağlayıcıyı küçük isteklere saklar.
max_input_tokens = 8000

[providers.nvidia]
base_url = "https://integrate.api.nvidia.com/v1"
api_key_env = "NVIDIA_API_KEY"
vision_models = ["meta/llama-4-*", "*vision*", "*-vl*"]
max_input_tokens = 128000

[providers.cerebras]
base_url = "https://api.cerebras.ai/v1"
api_key_env = "CEREBRAS_API_KEY"
vision_models = []
max_input_tokens = 30000

[providers.ollama]
base_url = "http://localhost:11434/v1"
api_key_env = ""
vision_models = ["*vl*", "llava*", "gemma3*"]
max_input_tokens = 32000

# Model bazlı ek parametreler (istek gövdesine eklenir).
[models."groq:openai/gpt-oss-20b"]
extra = { reasoning_effort = "low" }

[models."groq:openai/gpt-oss-120b"]
extra = { reasoning_effort = "medium" }

[models."cerebras:gpt-oss-120b"]
extra = { reasoning_effort = "low" }
'''


ENV_TEMPLATE = """# ALICE API anahtarları. Boş bıraktığın sağlayıcı atlanır.
# Google AI Studio  -> https://aistudio.google.com/apikey
GEMINI_API_KEY=
# OpenRouter        -> https://openrouter.ai/keys
OPENROUTER_API_KEY=
# Groq              -> https://console.groq.com/keys
GROQ_API_KEY=
# NVIDIA NIM        -> https://build.nvidia.com  (Developer Program, key nvapi- ile başlar)
NVIDIA_API_KEY=
# Cerebras          -> https://cloud.cerebras.ai
CEREBRAS_API_KEY=
# TypeSafe Jev (opsiyonel, ücretli ama çok ucuz) -> https://typesafe.ai
TYPESAFE_API_KEY=
"""


PERSONALITY_TEMPLATE = """# ALICE'in kişiliği

Bu dosya her oturumda ALICE'e okunur. İstediğin gibi değiştir.

- Hitap: bana adımla ya da "sen" diye hitap et, resmî olma.
- Ton: sakin ve sıcak; gerektiğinde şakacı. Acil durumlarda (uçuş uyarısı, hata) espri yapma, net ve kısa ol.
- Uzunluk: sesli cevaplarda 1-3 cümle; yazılı cevaplarda gerektiği kadar.
- Şaka sıklığı: ara sıra, işin önüne geçmeyecek kadar.
"""


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_env_file(path: Path) -> None:
    """Basit KEY=VALUE parser. Zaten tanımlı ortam değişkenlerinin üstüne yazmaz."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str = ""
    vision_models: list[str] = field(default_factory=list)
    max_input_tokens: int = 32000
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env) or None

    @property
    def needs_key(self) -> bool:
        return bool(self.api_key_env)

    @property
    def enabled(self) -> bool:
        return not self.needs_key or self.api_key is not None

    def supports_vision(self, model: str) -> bool:
        return any(fnmatch.fnmatch(model.lower(), p.lower()) for p in self.vision_models)


@dataclass
class Config:
    data: dict[str, Any]
    path: Path | None = None

    @property
    def agent(self) -> dict[str, Any]:
        return self.data.get("agent", {})

    @property
    def roles(self) -> dict[str, list[str]]:
        return self.data.get("roles", {})

    @property
    def decider(self) -> dict[str, Any]:
        return self.data.get("decider", {})

    def providers(self) -> dict[str, ProviderConfig]:
        out = {}
        for name, p in self.data.get("providers", {}).items():
            out[name] = ProviderConfig(
                name=name,
                base_url=p["base_url"].rstrip("/"),
                api_key_env=p.get("api_key_env", ""),
                vision_models=list(p.get("vision_models", [])),
                max_input_tokens=int(p.get("max_input_tokens", 32000)),
                headers=dict(p.get("headers", {})),
            )
        return out

    def model_extra(self, ref: str) -> dict[str, Any]:
        return dict(self.data.get("models", {}).get(ref, {}).get("extra", {}))


def set_config_value(section: str, key: str, value: Any) -> Path:
    """~/.alice/config.toml'da [section] altındaki key'i yaz (yoksa ekle). value TOML'a çevrilir."""
    import json
    import re

    init_home()
    path = alice_home() / "config.toml"
    text = path.read_text(encoding="utf-8")
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    sec = re.search(rf"(?m)^\[{re.escape(section)}\]\s*$", text)
    if not sec:
        text = text.rstrip() + f"\n\n[{section}]\n{line}\n"
    else:
        nxt = re.search(r"(?m)^\[", text[sec.end():])
        end = sec.end() + (nxt.start() if nxt else len(text) - sec.end())
        body = text[sec.end():end]
        rx = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
        body = rx.sub(lambda _m: line, body, count=1) if rx.search(body) else f"\n{line}" + body
        text = text[:sec.end()] + body + text[end:]
    path.write_text(text, encoding="utf-8")
    return path


def default_config_dict() -> dict[str, Any]:
    return tomllib.loads(DEFAULT_CONFIG_TOML)


def load_config(path: Path | None = None, cwd: Path | None = None) -> Config:
    home = alice_home()
    load_env_file((cwd or Path.cwd()) / ".env")
    load_env_file(home / ".env")
    data = default_config_dict()
    cfg_path = path or (home / "config.toml")
    if cfg_path.is_file():
        with open(cfg_path, "rb") as f:
            user = tomllib.load(f)
        # Rol listeleri birleştirilmez, kullanıcınınki tamamen geçerlidir.
        data = _deep_merge(data, user)
    return Config(data=data, path=cfg_path if cfg_path.is_file() else None)


def init_home(force: bool = False) -> list[Path]:
    home = alice_home()
    home.mkdir(parents=True, exist_ok=True)
    written = []
    cfg = home / "config.toml"
    if force or not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG_TOML.lstrip(), encoding="utf-8")
        written.append(cfg)
    env = home / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
        written.append(env)
    for sub in ("sessions", "browser-profile"):
        (home / sub).mkdir(exist_ok=True)
    pers = home / "personality.md"
    if not pers.exists():
        pers.write_text(PERSONALITY_TEMPLATE, encoding="utf-8")
        written.append(pers)
    mem = home / "ALICE.md"
    if not mem.exists():
        mem.write_text(
            "# Kişisel talimatlar\n\nBuraya ALICE'in her oturumda bilmesini istediğin şeyleri yaz.\n",
            encoding="utf-8",
        )
        written.append(mem)
    return written
