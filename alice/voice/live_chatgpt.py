"""ALICE Live (ChatGPT Realtime): CLIProxyAPI üzerinden sesten sese canlı konuşma.

CLIProxyAPI, ChatGPT (Codex) hesabıyla OpenAI Realtime uyumlu bir uç sunar (`/v1/realtime`). Bu modül o ucu
kullanır: mikrofon sesi 24 kHz PCM olarak akıtılır, modelin sesi aynı hızda çalınır, sunucu tarafı VAD ile
kullanıcı susunca cevap üretilir; kullanıcı araya girerse ses kesilir. ALICE'in araçları function calling ile
çağrılır (aynı izin/onay yolundan geçer).

Gemini Live (alice/voice/live.py) ile aynı arayüzü verir: VoiceAssistant `make_live` ile seçer.
"""

from __future__ import annotations

import array
import base64
import json
import queue
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult
from alice.voice.live import AudioOut, LiveError, LiveUI, sanitize_schema

REALTIME_PROMPT = """# Canlı sesli konuşma
Kullanıcıyla sesli, canlı konuşuyorsun. Doğal, sıcak ve kısa konuş (genelde 1-2 cümle), Türkçe.
Telaffuz: anadili Türkçe olan, İstanbul'da yaşayan genç bir kadın gibi konuş; yabancı aksan yok. Türkçe ünlüleri
(ı, ö, ü) ve r'yi Türk gibi söyle, vurguyu Türkçedeki gibi kelimenin sonuna yakın koy, cümleyi düz ve rahat bir
tonla, doğal hızda bitir.
Liste, kod, uzun yol ya da sayı okuma; gerekirse ekrana koyduğunu söyle.
Tek adımlık işleri (müzik/video çal, YouTube'da bir şey aç, uygulama ya da site aç, ses seviyesi, hatırlatma,
zamanlayıcı) doğrudan ilgili araçla hemen yap; bunlar için `think` çağırma, çok yavaştır.
Bilgi, açıklama, plan, kod, hesap, güncel bir şey ya da çok adımlı bir iş istenince `think` aracını çağır. Önce
tek kısa cümleyle ("Bir bakayım.") haber ver, sonra `think`in döndürdüğü sonucu kendi cümlelerinle, kısa ve
doğal biçimde anlat. Sonucu olduğu gibi okuma. Araç sonucunu görmeden "yaptım" deme.
Duyduğun metin konuşma tanımadan gelir ve kelimeler bozuk olabilir (ör. "Alice" adın "Aisla", "Alis" gibi
yazılabilir). İsteğin özünü anladıysan anlamsız kelimeyi yok say ve soru sormadan yap; yalnızca gerçekten
anlamadıysan tek kısa soru sor.
`think` bir soru sorduysa (güçlü modelin sana ihtiyacı olan bilgi) o soruyu kullanıcıya aynen ilet ve cevabını
bekle; cevabı yine `think` aracına istek olarak ver. Bir iş sürüyorsa aynı konuşmayı sürdür.
Sen hiçbir şey demeden araç çağırırsan kullanıcı kısa bir "Bir saniye." duyar; araç sonucundan sonra bunu
tekrarlama, doğrudan sonucu söyle.
Araçların vermediği sayıları ve durumları asla uydurma; bilmiyorsan söyle.
Mikrofon oyun/video/başkalarının konuşmasını da duyar; sana söylenmemiş, anlamsız ya da Türkçe olmayan
cümlelere cevap verme, sessiz kal. Kullanıcı Discord'da ya da oyunda arkadaşlarıyla da konuşur: cümle başka
birine hitap ediyorsa (bir isim, "gel buraya", "bak şimdi") ya da oyuna dair kendi kendine söylenmiş bir lafsa
hiç ses çıkarma. Sohbetin devamıysa (az önceki sorunun cevabı, "evet", "aç onu") cevap ver. Her zaman Türkçe
konuş."""

THINK_TOOL = {
    "type": "function",
    "name": "think",
    "description": ("Bilgi, açıklama, plan, kod, hesap, güncel bilgi ya da ALICE'in araçlarıyla yapılacak işleri "
                    "güçlü modele devret. Sonucu kullanıcıya kısa cümlelerle anlat; soru dönerse kullanıcıya ilet."),
    "parameters": {"type": "object", "properties": {
        "request": {"type": "string", "description": "Kullanıcının isteği; eksiksiz, olduğu gibi"}},
        "required": ["request"]},
}

# Canlı modelin doğrudan çağırdığı hızlı araçlar (config: [voice] live_realtime_tools, "*" = hepsi).
REALTIME_TOOLS = ("youtube", "media", "windows", "weather", "notes", "now", "spotify", "memory")

# Codex Realtime'ın kabul ettiği sesler (2026-11-06, sunucunun "Supported values" hatasından).
REALTIME_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")

HUSH_WORDS = ("sus", "sussana", "kes", "yeter", "boşver", "kapat")

_OPENAI_TYPES = {"OBJECT": "object", "STRING": "string", "INTEGER": "integer", "NUMBER": "number",
                 "BOOLEAN": "boolean", "ARRAY": "array", "NULL": "null"}


def openai_schema(schema: Any) -> Any:
    """ALICE araç şemasını OpenAI Realtime'ın istediği küçük harfli JSON Schema'ya çevir.

    ALICE'in iç şemaları Gemini için büyük harf (`STRING`, `INTEGER`, `OBJECT`) kullanıyor; Realtime API
    bunları reddedip `invalid_function_parameters` veriyor ve oturum güncellemesi tümden düşüyordu.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                out["type"] = _OPENAI_TYPES.get(value.upper(), value.lower())
            elif key == "type" and isinstance(value, list):
                types = [_OPENAI_TYPES.get(str(t).upper(), str(t).lower()) for t in value if t != "null"]
                out["type"] = types[0] if len(types) == 1 else types
            elif key == "nullable":
                continue  # OpenAI şeması "nullable" bilmiyor; alan zaten zorunlu değil
            else:
                out[key] = openai_schema(value)
        return out
    if isinstance(schema, list):
        return [openai_schema(item) for item in schema]
    return schema


def _close(pre: dict[str, Any]) -> None:
    """Önceden hazırlanmış (kullanılmayan) bağlantıyı kapat; hazırlık hâlâ sürüyorsa bitmesini bekle."""
    t = pre.get("thread")
    if t is not None and t.is_alive():
        t.join(RealtimeSession.PREFETCH_WAIT)
    ws = pre.get("ws")
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def _is_open(ws: Any) -> bool:
    """Isıtılmış bağlantı sunucu tarafından kapatılmış olabilir (zaman aşımı, proxy yeniden başladı)."""
    try:
        return getattr(ws.protocol.state, "name", "OPEN") == "OPEN"
    except Exception:  # noqa: BLE001 - durum okunamıyorsa dene; olmazsa converse hata verir
        return True


def resample_16_to_24(pcm: bytes) -> bytes:
    """16 kHz PCM16 -> 24 kHz PCM16 (doğrusal aradeğerleme). Küçük kareler için yeterli ve hızlı."""
    if not pcm:
        return b""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n = len(a)
    if n == 0:
        return b""
    out = array.array("h", [0]) * (n * 3 // 2)
    for i in range(len(out)):
        x = i * 2 / 3
        j = int(x)
        frac = x - j
        v = a[j] if j + 1 >= n else a[j] * (1 - frac) + a[j + 1] * frac
        out[i] = int(v)
    return out.tobytes()


class RealtimeSession:
    """ChatGPT Realtime oturumu (CLIProxyAPI üzerinden). `available` False ise Gemini/eski yol kullanılır."""

    TOOL_TIMEOUT = 300.0
    PREFETCH_WAIT = 10.0

    def __init__(self, cfg, agent, log: Callable[[str], None] = lambda s: None,
                 out_factory: Callable[[], Any] = AudioOut, connect: Callable[..., Any] | None = None):
        v = cfg.data.get("voice", {})
        self.cfg = cfg
        self.agent = agent
        self.log = log
        self.model = str(v.get("live_realtime_model") or "gpt-realtime-2.1")
        self.voice = str(v.get("live_realtime_voice") or "marin").strip().lower()
        if self.voice not in REALTIME_VOICES:
            # Geçersiz ses adı (ör. ChatGPT uygulamasındaki "quip") session.update'in tamamını düşürüyor:
            # talimatlar ve araçlar da uygulanmıyordu.
            log(f"realtime: '{self.voice}' sesi desteklenmiyor, marin kullanılıyor")
            self.voice = "marin"
        self.idle = min(float(v.get("live_idle", 5)), 5)
        self.silence_ms = int(v.get("live_silence_ms", 600))
        # Hoparlör kullanılırken ALICE'in kendi sesi mikrofona geri dönebiliyor. Realtime VAD bunu yeni
        # kullanıcı turu sanıp devam eden cevabı iptal eder; bu yüzden varsayılan olarak cevap sırasında
        # mikrofonu sunucuya göndermiyoruz. Kulaklık kullananlar eski "araya girerek kes" davranışını açabilir.
        self.echo_guard = bool(v.get("live_realtime_echo_guard", True))
        self.tools_on = bool(v.get("live_tools", True))
        # Hızlı ses modeli zayıf: bilgi/plan/kod/araç işlerini güçlü modele devreden think aracı.
        self.think = bool(v.get("live_think", True))
        # think'in model sırası (Gemini yoluyla aynı ayar). Eskiden ses ajanının kendi zinciri kullanılıyordu:
        # başındaki model takılınca (6 Kasım: gemini-flash-latest ReadTimeout) tek bir "video aç" ~95 sn sürdü.
        self.think_models = [str(r) for r in v.get("live_think_models") or []]
        # Canlı modele sadece hızlı tek adımlık işlerin araçları gider; gerisi think ile güçlü modele. 69 aracın
        # şeması (~112 KB) her oturumda session.update'i ~1 sn, ilk sesi ~1 sn geciktiriyordu (6 Kasım ölçümü).
        tools = v.get("live_realtime_tools", REALTIME_TOOLS)
        self.allowed_tools: set[str] | None = None if tools == "*" or tools == ["*"] else {str(t) for t in tools}
        # ChatGPT sesi kendisi duyar: yerel Whisper'ın dökümünü beklemek (≤1,5 sn) ve ses yerine yanlış yazılmış
        # metni göndermek yerine ses gider. Eski davranış: live_realtime_text_hint = true.
        self.prefers_audio = not bool(v.get("live_realtime_text_hint", False))
        # ChatGPT'nin kendi döküm modeli: duyduğu loglara ve geçmişe yazılır, "Alice sus" sesli yolda da çalışır.
        self.transcribe = str(v.get("live_realtime_transcribe", "gpt-4o-mini-transcribe") or "")
        self.language = str(v.get("language", "tr") or "tr")
        # Cevap bitince bir sonraki oturum arka planda hazırlanır (bağlantı + ayar ≈ 2 sn konuşmaya binmesin).
        self.prewarm = bool(v.get("live_realtime_prewarm", True))
        self._think_lock = threading.Lock()
        self.gated = False
        self.hushed = False
        self.last_active = 0.0
        self.single_turn = False
        self.declined = False  # son devam cümlesi ALICE'e söylenmemişti (Jev): takip penceresi açılmaz
        # Araya girme: ALICE konuşurken gelen kare kullanıcının sesi olarak doğrulandıysa (ses izi, VoiceAssistant
        # ayarlar) cevap kesilir. Doğrulama yoksa yankı yüzünden kapalı (echo_guard).
        self.barge_ok: Callable[[], bool] = lambda: False
        # Telefon araması (alice/app/call.py): duyulan ve söylenen metin ekranda gösterilsin.
        self.on_heard: Callable[[str], None] | None = None
        self.on_said: Callable[[str], None] | None = None
        self.hud_on = True  # telefon araması bilgisayardaki HUD'u oynatmasın
        self.announce: Callable[[str], None] | None = None
        self.on_correction: Callable[[str], None] | None = None
        self._out_factory = out_factory
        self._connect = connect
        self._send_update = False
        self._out = None
        self._ws = None
        self._stop = None
        self._tools_q: queue.Queue | None = None
        self._pre: dict[str, Any] = {}
        self._pre_lock = threading.Lock()
        self._proxy = self._read_proxy()
        # Her "Alice"te (Discord/oyunda her cümlede) yeni oturum açılıyor; önceki konuşma talimata eklenmezse model
        # her cümleyi bağlamsız duyuyordu ("herhangi bir şey açarsan olur" -> neyi?).
        self.history_minutes = float(v.get("live_history_minutes", 30))
        self._history: list[tuple[float, str, str]] = []

    # ------------------------------------------------------------------ hazırlık
    @staticmethod
    def _read_proxy() -> dict[str, str] | None:
        """Yerel CLIProxyAPI ayarı: taban adres ve istemci anahtarı (yoksa None)."""
        try:
            from alice.app import cliproxy

            s = cliproxy.settings()
            if not s.get("installed"):
                return None
            key = ""
            for line in (s["dir"] / "keys.txt").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("api_key="):
                    key = line.split("=", 1)[1].strip()
            if not key:
                return None
            return {"base": f"http://127.0.0.1:{s['port']}", "key": key}
        except Exception:  # noqa: BLE001 - proxy kurulu değilse sessizce yok say
            return None

    @property
    def available(self) -> bool:
        if self._connect is not None:
            return True
        if not self._proxy:
            return False
        try:
            from websockets.sync.client import connect  # noqa: F401
        except ImportError:
            return False
        return bool(self.model)

    @property
    def playing(self) -> bool:
        out = getattr(self, "_out", None)
        return bool(out is not None and getattr(out, "playing", False))

    def _instructions(self) -> str:
        base = ""
        try:
            base = str(self.agent.messages[0].get("content") or "")
        except Exception:  # noqa: BLE001
            pass
        refresh = getattr(self.agent, "refresh_memory", None)
        if callable(refresh):
            try:
                refresh(force=True)
            except Exception:  # noqa: BLE001
                pass
        history = self.history_text()
        tail = ("\n\n# Az önceki konuşma (aynı sohbetin devamı; kısa cevaplar buna göre anlaşılır)\n" + history
                if history else "")
        try:
            # Sistem istemindeki tarih saatlerce açık kalan süreçte eskiyor: telefon araması "bugün 6 Kasım Cuma"
            # dedi (7 Kasım Cumartesi'ydi). Yazılı yol bunu her cümleye ekliyor (with_clock), canlı yol da eklesin.
            from alice.voice.assistant import with_clock

            tail = "\n\n# Şu an\n" + with_clock("").strip() + tail
        except Exception:  # noqa: BLE001
            pass
        # Geçmiş ve canlı konuşma kuralları kesilmesin: kırpma sistem talimatından yapılır.
        room = 12000 - len(REALTIME_PROMPT) - len(tail) - 2
        return base[:max(0, room)] + "\n\n" + REALTIME_PROMPT + tail

    def history_text(self, limit: int = 8) -> str:
        from datetime import datetime

        cutoff = time.time() - self.history_minutes * 60
        lines = []
        for ts, user, reply in [h for h in self._history if h[0] >= cutoff][-limit:]:
            line = f"- {datetime.fromtimestamp(ts):%H:%M} Kullanıcı: {user[:300]}"
            if reply:
                line += f" / Sen: {reply[:300]}"
            lines.append(line)
        return "\n".join(lines)

    def _remember(self, heard: str, said: str, tools: list[str]) -> None:
        heard, said = heard.strip(), said.strip()
        if heard or said or tools:
            extra = f" araçlar: {', '.join(tools)}" if tools else ""
            self.log(f"realtime duyulan: {heard[:300]} | cevap: {said[:400]}{extra}")
        if heard:
            self._history = (self._history + [(time.time(), heard, said)])[-20:]
        elif said and self._history:
            ts, user, before = self._history[-1]
            self._history[-1] = (ts, user, (before + " " + said).strip())

    def _tool_specs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [THINK_TOOL] if self.think else []
        if not self.tools_on:
            return out
        tools = getattr(self.agent, "tools", None)
        if isinstance(tools, dict):
            raw = [t.spec() for name, t in tools.items() if not name.startswith("mcp__") and name != "load_tools"
                   and (self.allowed_tools is None or name in self.allowed_tools)]
        else:
            try:
                raw = self.agent.tool_specs()
            except Exception:  # noqa: BLE001
                return out
        for spec in raw:
            f = spec.get("function", spec)
            if not f.get("name"):
                continue
            item: dict[str, Any] = {"type": "function", "name": f["name"],
                                    "description": str(f.get("description") or "")[:1000]}
            params = openai_schema(sanitize_schema(f.get("parameters") or {}))
            if params.get("properties"):
                item["parameters"] = params
            out.append(item)
        return out

    def _session_config(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "type": "realtime",
            "model": self.model,
            "instructions": self._instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000},
                          "turn_detection": {"type": "server_vad", "threshold": 0.5,
                                             "prefix_padding_ms": 300, "silence_duration_ms": self.silence_ms,
                                             "interrupt_response": not self.echo_guard}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.voice},
            },
        }
        if self.transcribe:
            session["audio"]["input"]["transcription"] = {"model": self.transcribe, "language": self.language}
        tools = self._tool_specs()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return {"expires_after": {"anchor": "created_at", "seconds": 900}, "session": session}

    def _secret(self) -> str:
        if not self._proxy:
            raise LiveError("yerel Claude Code proxy'si (CLIProxyAPI) ayarlı değil")
        # CLIProxyAPI'nin /v1/realtime/client_secrets ucu 64 KiB gövde kabul ediyor; ALICE'in talimatı ve
        # araç şemaları bunu aşınca 413 dönüyor ve canlı oturum hiç açılmıyordu (uzun/robotik yedek yol).
        # Bunun yerine proxy API anahtarıyla bağlanıp oturumu WebSocket üzerinden güncelle: aynı oturum,
        # boyut sınırı yok.
        self._send_update = True
        return str(self._proxy["key"])

    def _ws_url(self) -> str:
        base = (self._proxy or {}).get("base", "http://127.0.0.1:8317")
        ws = base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws}/v1/realtime?model={self.model}"

    # ------------------------------------------------------------------ ön hazırlık
    WARM_MAX = 60.0  # sn: ısıtılmış oturum bundan eskiyse kapatılır (kullanılmayan bağlantı açık kalmasın)

    def _open(self) -> Any:
        secret = self._secret()
        if self._connect is not None:
            return self._connect(self._ws_url(), secret)
        from websockets.sync.client import connect

        try:
            return connect(self._ws_url(), additional_headers={"Authorization": f"Bearer {secret}"},
                           open_timeout=10, max_size=None)
        except Exception as e:  # noqa: BLE001
            raise LiveError(f"realtime bağlanamadı: {e}") from e

    def prefetch(self, owner: Any = None) -> None:
        """Oturumu önceden ısıt: bağlan, session.created'ı al, ayarları (talimat, araçlar, ses) gönder.
        Uyanma kontrolüyle aynı anda ve her cevaptan sonra çalışır; konuşma başlayınca bu ~2 sn beklenmez
        (ölçüm: bağlantı ~0,9 sn + ayar ~1 sn)."""
        if not self.available:
            return
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {"owner": owner, "t": time.monotonic()}

            def run() -> None:
                try:
                    ws = self._open()
                    ws.recv(timeout=10)  # session.created
                    ws.send(json.dumps({"type": "session.update", "session": self._session_config()["session"]},
                                       ensure_ascii=False))
                    pre["ws"] = ws
                except Exception as e:  # noqa: BLE001
                    pre["error"] = e

            def expire() -> None:
                with self._pre_lock:
                    if self._pre is not pre:
                        return
                    self._pre = {}
                _close(pre)

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-rt-pre")
            self._pre = pre
            pre["thread"].start()
            timer = threading.Timer(self.WARM_MAX, expire)
            timer.daemon = True
            timer.start()

    def discard(self, owner: Any = None) -> None:
        """Uyandırma çıkmadı: o cümle için açılan bağlantıyı kapat. Başkasının (ör. cevap sonrası ısıtılan)
        oturumuna dokunma."""
        with self._pre_lock:
            if not self._pre or (owner is not None and self._pre.get("owner") is not owner):
                return
            pre, self._pre = self._pre, {}
        threading.Thread(target=_close, args=(pre,), daemon=True).start()

    def _take(self) -> tuple[Any, bool]:
        """(bağlantı, ısıtılmış mı). Isıtılmışsa ayarlar zaten gönderildi."""
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            pre["thread"].join(self.PREFETCH_WAIT)
            ws = pre.get("ws")
            if ws is not None and time.monotonic() - pre["t"] < self.WARM_MAX and _is_open(ws):
                return ws, True
            _close(pre)
            if pre.get("error"):
                self.log(f"realtime ön hazırlık başarısız: {pre['error']}")
        return self._open(), False

    # ------------------------------------------------------------------ ana akış
    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"", hint: Callable[[], str | None] | None = None,
                 ended: float | None = None, need_wake: bool = False, check_addressed: bool = False) -> None:
        """Mikrofon karelerini (16 kHz, 30 ms) modele akıt, cevabı çal, araçları çalıştır. Sessizlikte döner."""
        if hint is not None and not first_text and not self.prefers_audio:
            try:
                first_text = hint() or ""
            except Exception:  # noqa: BLE001
                first_text = ""
        t0 = time.monotonic()
        ws, warm = self._take()
        out = self._out_factory()
        self._ws, self._out, self._stop = ws, out, stop
        ui = LiveUI(self.agent.ui)
        old_ui, self.agent.ui = self.agent.ui, ui
        self.hushed = False
        state = {"active": time.monotonic(), "heard": "", "said": "", "calls": {}, "done": False,
                 "answered": False, "want_response": False, "output_until": 0.0,
                 "echo_notice": False, "finish": False, "said_parts": [], "tools": [],
                 # Discord'daki devam cümlesi: cevap ancak Jev "ALICE'e söylendi" derse üretilir (addressed.py).
                 "gate": "pending" if check_addressed else "open", "gate_until": time.monotonic() + 8.0,
                 "check": check_addressed, "muted": False, "responding": False, "barged": False}
        self.declined = False
        self.log(f"realtime oturumu açıldı [{self.model}] {'ısıtılmış' if warm else 'yeni bağlantı'} "
                 f"{time.monotonic() - t0:.2f} sn")
        self._hud("listen", "")

        def touch() -> None:
            state["active"] = time.monotonic()
            self.last_active = state["active"]

        def send(obj: dict[str, Any]) -> None:
            try:
                ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                raise LiveError(f"realtime gönderilemedi: {e}") from e

        def send_pcm(pcm: bytes) -> None:
            if pcm:
                send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

        def say_text(text: str) -> None:
            send({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}})
            send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
            touch()

        tool_thread: threading.Thread | None = None
        try:
            if not warm:
                ws.recv()  # session.created (ya da hata); ayrıntısına gerek yok
                if self._send_update:
                    # Talimatlar, araçlar ve ses: HTTP gövdesi yerine oturum açıldıktan sonra gönderilir.
                    send({"type": "session.update", "session": self._session_config()["session"]})
            if state["gate"] == "pending":
                td = self._session_config()["session"]["audio"]["input"]["turn_detection"]
                send({"type": "session.update", "session": {"type": "realtime", "audio": {"input": {
                    "turn_detection": {**td, "create_response": False}}}}})
            if first_text:
                say_text(first_text)
                pending_audio = b""
            else:
                pending_audio = resample_16_to_24(first_audio)
            while not stop.is_set() and not state["done"]:
                while True:  # mikrofondan biriken kareler (16 kHz -> 24 kHz)
                    try:
                        frame = frames.get_nowait()
                    except queue.Empty:
                        break
                    if frame:
                        # Ses çalarken yalnızca mikrofondaki hoparlör yankısını Realtime'a geri verirdik.
                        # VAD'in yeni tur açıp response'u yarıda bırakmasına yol açan asıl döngü buydu.
                        # Kareleri burada atmak, eski sesin konuşma bitince yeni bir istek gibi işlenmesini de
                        # engeller. ``output_until`` ağ paketleri arasındaki kısa boşlukları kapsar.
                        if self.echo_guard and (self.playing or time.monotonic() < state["output_until"]):
                            if not (any(frame) and self.barge_ok()):
                                continue
                            if not state["barged"]:
                                # Ses izi doğruladı: kullanıcı ALICE'in sözünü kesiyor (eskiden konuşurken gelen her
                                # kare atılıyordu; araya girmek de "Alice sus" da işlemiyordu, 7 Kasım). Bir cevap
                                # için bir kez: ses kesildikten sonra çalıcı kısa süre "çalıyor" görünüyor, her
                                # karede yeniden iptal gönderiliyordu (onlarca response_cancel_not_active).
                                self.log("realtime: kullanıcı araya girdi, ALICE susuyor")
                                out.clear()
                                if state["responding"]:
                                    send({"type": "response.cancel"})
                                state.update(output_until=0.0, finish=False, muted=state["responding"],
                                             barged=True)
                                if state["check"]:  # Discord: yeni cümle de ALICE'e mi söylendi, Jev baksın
                                    state.update(gate="pending", gate_until=time.monotonic() + 8.0)
                        send_pcm(resample_16_to_24(frame))
                        # Sessizlik kareleri oturumu açık tutmasın: 5 sn kimse konuşmazsa kapanır.
                        if any(frame):
                            touch()
                if pending_audio:
                    send_pcm(pending_audio)
                    pending_audio = b""
                try:
                    raw = ws.recv(timeout=0.05)
                except TimeoutError:
                    raw = None
                except Exception as e:  # noqa: BLE001 - bağlantı koptu
                    raise LiveError(f"realtime bağlantısı koptu: {e}") from e
                if raw is None:
                    if self.playing:
                        # Sessizlik penceresi ALICE sustuktan sonra başlasın. Model sesi gerçek zamandan hızlı
                        # gönderdiği için son parça çok önce gelmiş olur; eskiden ses biter bitmez oturum kapanıyor,
                        # kullanıcının cevabı yeni (bağlamsız, yavaş) bir oturuma gidiyordu.
                        touch()
                        continue
                    if tool_thread is not None and tool_thread.is_alive():
                        # Araç/think sürüyor: sessizlik sayma. Eskiden 12 sn'de oturum kapanıyor, think'in sonucu
                        # kapalı bağlantıya gidip kayboluyordu ("eee açacaktın").
                        touch()
                        continue
                    if state["gate"] == "pending" and time.monotonic() > state["gate_until"]:
                        self.log("realtime: devam cümlesinin dökümü gelmedi, cevap yok")
                        self.declined = True
                        break
                    if state["finish"]:
                        break  # tek cevaplık tur (Discord/oyun): cevap sonuna kadar çalındı
                    if time.monotonic() - state["active"] > self.idle:
                        self.log("realtime: sessizlik, oturum kapanıyor")
                        break
                    continue
                ev = self._decode(raw)
                kind = ev.get("type", "")
                if kind == "response.created":
                    state.update(muted=False, responding=True, barged=False)  # yeni cevap (araya girildiyse de)
                elif kind in ("response.output_audio.delta", "response.audio.delta") and state["muted"]:
                    continue  # kesilen cevabın yoldaki parçaları
                elif kind in ("response.output_audio.delta", "response.audio.delta"):
                    try:
                        out.put(base64.b64decode(ev.get("delta", "")))
                    except Exception:  # noqa: BLE001
                        pass
                    state["output_until"] = time.monotonic() + 0.35
                    if not state["answered"]:
                        # Gecikme ölçümü: kullanıcı sustuktan (yoksa oturum isteğinden) ilk sese kadar.
                        start = ended if isinstance(ended, (int, float)) and ended > 0 else t0
                        self.log(f"realtime ilk ses: {time.monotonic() - start:.2f} sn")
                    state["answered"] = True
                    touch()
                elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                    state["said"] += ev.get("delta", "")
                    touch()
                elif kind == "response.output_audio_transcript.done" or kind == "response.audio_transcript.done":
                    # Araçtan önceki "Bir bakayım." ile sonraki cevap ayrı cevaplar: ikisi de kayda girsin.
                    part = str(ev.get("transcript") or state["said"]).strip()
                    if part:
                        state["said_parts"].append(part)
                        if self.on_said is not None:
                            try:
                                self.on_said(part)
                            except Exception:  # noqa: BLE001
                                pass
                    state["said"] = ""
                elif kind == "conversation.item.input_audio_transcription.completed":
                    text = str(ev.get("transcript") or "").strip()
                    if text:
                        state["heard"] += " " + text
                        ui.heard(text + " ")
                        self.log(f"realtime duydu: {text[:160]}")
                        if self.on_heard is not None:
                            try:
                                self.on_heard(text)
                            except Exception:  # noqa: BLE001 - ekran hatası konuşmayı bozmasın
                                pass
                        if self._is_hush(text):
                            self.hushed = True
                            out.clear()
                            state["done"] = True
                        elif state["gate"] == "pending":
                            if self._addressed(text):
                                state["gate"] = "open"
                                send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
                            else:
                                self.declined = True
                                state["done"] = True
                        touch()
                elif kind == "input_audio_buffer.speech_started":
                    # Yankı koruması açıkken bu olay çoğunlukla önceki birkaç mikrofon karesinden gelir.
                    # Cevabı temizleme ya da konuşma zamanını uzatma: ikisi de cümleyi yarıda kesiyordu.
                    if self.echo_guard and (self.playing or time.monotonic() < state["output_until"]):
                        if not state["echo_notice"]:
                            self.log("realtime: çalarken algılanan ses yankı korumasıyla yok sayıldı")
                            state["echo_notice"] = True
                        continue
                    if not self.echo_guard and self.playing:
                        # Yankı koruması kapalı (kulaklık, telefonun yankı engelleyicisi): sunucu cevabı iptal eder
                        # ama önceden gelmiş ses çalmaya devam ediyordu; kullanıcı konuşunca hemen sus.
                        out.clear()
                    touch()
                elif kind in ("response.function_call_arguments.done",):
                    args = str(ev.get("arguments") or "{}")
                    state["tools"].append(f"{ev.get('name') or '?'}({args[:120]})")
                    state["calls"][str(ev.get("call_id") or "")] = {
                        "id": str(ev.get("call_id") or ""), "type": "function",
                        "function": {"name": str(ev.get("name") or ""), "arguments": str(ev.get("arguments") or "{}")}}
                elif kind == "response.done":
                    state["responding"] = False
                    status = str((ev.get("response") or {}).get("status") or "")
                    if status and status not in ("completed", "in_progress"):
                        self.log(f"realtime cevap erken bitti: {status}")
                    calls = [c for c in state["calls"].values() if c["function"]["name"]]
                    state["calls"] = {}
                    if calls:
                        if tool_thread is not None and tool_thread.is_alive():
                            tool_thread.join(5)
                        tool_thread = threading.Thread(target=self._handle_calls, args=(calls, send), daemon=True)
                        tool_thread.start()
                    else:
                        if self.single_turn and state["answered"] and status != "cancelled":
                            # response.done üretimin bittiğini söyler, çalmanın değil: burada çıkınca close()
                            # kuyruktaki sesi atıyor, ALICE cümlenin ortasında susuyordu. Ses bitince çıkılır.
                            state["finish"] = True
                        if not self.playing:
                            touch()
                elif kind == "session.created" or kind == "session.updated":
                    continue
                elif kind == "error":
                    self.log(f"realtime hata: {json.dumps(ev.get('error') or ev, ensure_ascii=False)[:300]}")
                    info = ev.get("error") or {}
                    err = str(info.get("code") or info.get("type") or "")
                    msg = str(info.get("message") or "")
                    if err in ("invalid_api_key", "session_expired"):
                        raise LiveError(f"realtime oturumu geçersiz: {err}. {msg[:120]}")
                    if any(k in (err + msg).lower() for k in ("rate limit", "quota", "exhausted", "billing", "429")):
                        # Kota/limit doldu: her "Hey Alice"te yeniden denemek yerine VoiceAssistant
                        # bir saat normal sesli yola döner (LiveError.limited bunu message'dan anlar).
                        raise LiveError(f"realtime quota/rate limit: {err}. {msg[:160]}")
                    if err in ("invalid_model",):
                        raise LiveError(f"realtime modeli desteklenmiyor: {msg[:160]}")
        finally:
            self.agent.ui = old_ui
            try:
                if not self.declined:  # arkadaşa söylenen cümle sohbet geçmişine girmesin
                    self._remember(f"{first_text} {state['heard']}", " ".join(state["said_parts"] + [state["said"]]),
                                   state["tools"])
            except Exception:  # noqa: BLE001 - kayıt konuşmayı bozmasın
                pass
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            if not self.hushed and not stop.is_set():
                # Bağlantı koptuysa ya da oturum başka sebeple bittiyse de gelmiş ses sonuna kadar çalınsın;
                # close() kuyruğu boşaltır. "Alice sus" ve durdurma beklemeden keser.
                drain = getattr(out, "drain", None)
                if callable(drain):
                    try:
                        drain(60.0, stop.is_set)
                    except Exception:  # noqa: BLE001
                        pass
            try:
                out.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws, self._out = None, None
            self._hud("idle", "")
            if self.prewarm and self._connect is None and not self.hushed and not stop.is_set():
                # Takip cümlesi ("Alice" demeden devam) bağlantı + ayar beklemesin: bir sonraki oturum hazır dursun.
                try:
                    self.prefetch()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ dışarıdan (telefon araması)
    def inject(self, text: str) -> bool:
        """Açık oturuma yazılı mesaj ver (aramada \"Yaz\"). Oturum yoksa False."""
        ws = self._ws
        if ws is None or not text.strip():
            return False
        try:
            ws.send(json.dumps({"type": "conversation.item.create", "item": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": text}]}}, ensure_ascii=False))
            ws.send(json.dumps({"type": "response.create", "response": {"output_modalities": ["audio"]}}))
            return True
        except Exception:  # noqa: BLE001
            return False

    def interrupt(self) -> None:
        """ALICE'i sustur (aramada küreye dokunma): çalan ses kesilir, süren cevap iptal edilir."""
        out, ws = self._out, self._ws
        if out is not None:
            out.clear()
        if ws is not None:
            try:
                ws.send(json.dumps({"type": "response.cancel"}))
            except Exception:  # noqa: BLE001 - sürmekte olan cevap yoksa sunucu hata döner, önemsiz
                pass

    # ------------------------------------------------------------------ yardımcılar
    def _addressed(self, text: str) -> bool:
        """Devam cümlesi ALICE'e mi söylendi (Jev). Ulaşılamazsa evet: konuşma kilitlenmesin."""
        from alice.voice.addressed import is_addressed

        last = self._history[-1][2] if self._history else ""
        ok, p = is_addressed(self.cfg, text, last, log=self.log)
        if p is not None:
            who = "ALICE'e" if ok else "arkadaşlara/kendine, cevap yok"
            self.log(f"hitap kontrolü: {who} (p={p:.2f})")
        return ok

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        try:
            got = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    @staticmethod
    def _is_hush(text: str) -> bool:
        low = text.lower().strip()
        return any(w in low.split() for w in HUSH_WORDS) if low else False

    def _hud(self, state: str, detail: str) -> None:
        if not self.hud_on:
            return
        try:
            from alice.hud import views

            views.voice_state(state, detail)
        except Exception:  # noqa: BLE001 - HUD yoksa konuşma bozulmasın
            pass

    def _handle_calls(self, calls: list[dict[str, Any]], send: Callable[[dict[str, Any]], None]) -> None:
        """Araçları çalıştır, sonuçları modele geri yaz, cevabı yeniden üret."""
        outputs = []
        thought = ""
        for call in calls:
            res = self._run_tool(call)
            text = res.text if isinstance(res, ToolResult) else str(res)
            if call.get("function", {}).get("name") == "think" and not getattr(res, "is_error", False):
                thought = text
            outputs.append({"type": "function_call_output", "call_id": call["id"],
                            "output": json.dumps({"ok": not getattr(res, "is_error", False),
                                                  "result": text[:4000]}, ensure_ascii=False)})
        try:
            for item in outputs:
                send({"type": "conversation.item.create", "item": item})
            send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
        except Exception as e:  # noqa: BLE001
            self.log(f"realtime: araç sonucu gönderilemedi: {e}")
            # Oturum bu arada kapandı (bağlantı koptu, kullanıcı sustu): think'in cevabı kaybolmasın.
            if thought and callable(self.announce):
                try:
                    self.announce(thought[:600])
                except Exception:  # noqa: BLE001
                    pass

    def _run_tool(self, call: dict[str, Any]) -> ToolResult:
        if call.get("function", {}).get("name") == "think":
            return self._think(str(call["function"].get("arguments") or "{}"))
        if self._tools_q is None:
            q: queue.Queue = queue.Queue()
            self._tools_q = q
            threading.Thread(target=self._tool_worker, args=(q,), daemon=True, name="alice-live-rt-tools").start()
        job: dict[str, Any] = {"call": call, "done": threading.Event()}
        self._tools_q.put(job)
        if not job["done"].wait(self.TOOL_TIMEOUT):
            self.log(f"realtime araç takıldı: {call['function']['name']}")
            return ToolResult(f"{call['function']['name']} {self.TOOL_TIMEOUT:.0f} sn içinde bitmedi", is_error=True)
        return job["res"]

    def _think(self, raw: str) -> ToolResult:
        """Hızlı ses modelinin işi güçlü modele devretmesi: ALICE ajanı isteği kendi araçlarıyla yürütür.

        Ajanın cevabı (bir soru da olabilir) modele döner; canlı model onu kullanıcıya söyler. Aynı ajan
        nesnesi kullanıldığı için sonraki `think` çağrıları konuşmayı kaldığı yerden sürdürür."""
        try:
            args = json.loads(raw or "{}")
            request = str(args.get("request") or "").strip()
        except (TypeError, ValueError):
            return ToolResult("think: argüman geçerli JSON değil", is_error=True)
        if not request:
            return ToolResult("think: istek boş", is_error=True)
        if not self._think_lock.acquire(timeout=2):
            return ToolResult("think: önceki istek hâlâ çalışıyor, bitince tekrar sor", is_error=True)
        try:
            text = request
            try:
                from alice.voice.assistant import with_clock

                text = with_clock(request)
            except Exception:  # noqa: BLE001 - saat eklenemezse istek olduğu gibi gider
                pass
            old_role = getattr(self.agent, "role", None)
            role = self._think_role(request)
            t0 = time.monotonic()
            try:
                if role:
                    self.agent.role = role
                reply = str(self.agent.run_turn(text) or "").strip()
            except Exception as e:  # noqa: BLE001 - canlı konuşma düşmesin
                return ToolResult(f"think hatası: {type(e).__name__}: {e}", is_error=True)
            finally:
                if role:
                    self.agent.role = old_role
                self.log(f"realtime think {time.monotonic() - t0:.1f} sn [{role or old_role}]")
            return ToolResult(reply or "Güçlü model boş cevap döndürdü.", is_error=not reply)
        finally:
            self._think_lock.release()

    THINK_ROLE = "live-think"

    def _think_role(self, request: str = "") -> str | None:
        """think için geçici rol: live_think_models, boşsa ses için ölçülmüş hızlı modeller + beyin zinciri.
        Jev yönlendirmesi açıksa ağır işler ([routing] voice_strong, ör. Sonnet 5.5) öne alınır."""
        router = getattr(self.agent, "router", None)
        if router is None or not hasattr(router, "role_overrides"):
            return None
        try:
            refs = list(self.think_models) or (router.auto_chain("voice")[:3] + router.chain("brain"))
        except Exception:  # noqa: BLE001 - ölçüm/rol bilgisi yok: ajanın kendi zinciri
            return None
        chain = [r for i, r in enumerate(refs) if r not in refs[:i]]
        if not chain:
            return None
        if request:
            try:
                from alice.providers import jev_route

                chain, r = jev_route.voice_chain(router, self.cfg, request, chain, log=self.log)
                self.log(f"realtime think yönlendirme: {r.tier} ({r.reason}) -> {chain[0]}")
            except Exception as e:  # noqa: BLE001 - yönlendirme düşerse hızlı sıra
                self.log(f"realtime think yönlendirme hatası: {e}")
        router.role_overrides[self.THINK_ROLE] = chain
        return self.THINK_ROLE

    def _tool_worker(self, q: "queue.Queue[dict[str, Any]]") -> None:
        try:
            import comtypes  # UI Automation bu iş parçacığında da çalışsın

            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        while True:
            job = q.get()
            call = job["call"]
            try:
                self.agent._enable_groups_for(tool_names=(call["function"]["name"],))
                job["res"] = self.agent._execute_calls([call])[0]
            except Exception as e:  # noqa: BLE001
                job["res"] = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
            job["done"].set()
