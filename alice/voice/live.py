"""Gemini Live: sesten sese canlı konuşma (ChatGPT'nin sesli modunun Google karşılığı).

"Hey Alice" duyulunca bir Live oturumu açılır; mikrofon doğrudan modele akar, model Kore sesiyle ~0,5 sn'de
konuşmaya başlar, araya girilince susar (sunucu tarafı VAD). ALICE'in araçları (YouTube açma, pencere, not...)
function calling ile çalışır. Konuşma `idle` saniye sessiz kalınca oturum kapanır, tekrar "Hey Alice" beklenir.
Bağlanamazsa (internet, kota, paket yok) çağıran eski yola (yazıya çevir -> model -> ses) döner.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}")
DEFAULT_MODELS = ["gemini-3.8-live", "gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]
LIVE_GROUPS = {"desktop", "browser", "personal", "system", "automation"}
YES = ("evet", "tamam", "olur", "onay", "yap", "devam", "peki")
SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items", "nullable", "format"}

LIVE_PROMPT = """# Canlı sesli konuşma
Kullanıcıyla sesli, canlı konuşuyorsun. Doğal, sıcak ve kısa konuş (genelde 1-2 cümle), Türkçe.
Liste, kod, yol ya da uzun sayı okuma; gerekirse ekrana koyduğunu söyle.
Bir iş istenince ilgili aracı hemen çağır.
Web işlerini her zaman kullanıcının açık tarayıcısında, açık sekmelerinde yap; sıfırdan pencere açma.
YouTube'da video açma, "ilk videoya tıkla", "ikinci dakikaya getir" için youtube aracı (play, at=saniye; seek).
Diğer sitelerde önce browser aracı: kullanıcının tarayıcısına bağlanır (tabs, switch_tab; goto açık site sekmesini
kullanır; snapshot/click/type ile sayfada iş yapar). browser "bağlanılamıyor" derse: windows action=switch_tab
target=<site adı> ile açık sekmeye geç, windows action=navigate target=<URL ya da arama> ile git.
Yeni sekme ya da ayrı pencere sadece kullanıcı isterse.
Bir araç "onay gerekli" derse kullanıcıya kısaca sor; "evet" derse aynı çağrıyı tekrarla.
İç düşüncelerini, planını, araç hatalarını ya da düzeltmelerini asla sesli söyleme; sadece kullanıcıya Türkçe konuş.
Şu an: {now}."""


# Modelin sesli okuduğu iç notları ("TIRED: I made a syntax error..." gibi) yakalar.
_INTERNAL_TAG = re.compile(r"\b[A-Z]{4,}\s*:")
_INTERNAL_EN = re.compile(r"`|\b(function call|syntax error|tool call|I made|I need to|I will call|let me call)\b", re.I)


def looks_internal(text: str) -> bool:
    """Cevap metni kullanıcıya değil modelin kendisine yazılmış bir not mu?"""
    return bool(_INTERNAL_TAG.search(text) or _INTERNAL_EN.search(text))


class LiveError(Exception):
    pass


def sanitize_schema(s: Any) -> Any:
    """JSON Schema -> Gemini'nin kabul ettiği alt küme (additionalProperties, default vb. atılır)."""
    if not isinstance(s, dict):
        return s
    out: dict[str, Any] = {}
    for k, v in s.items():
        if k not in SCHEMA_KEYS:
            continue
        if k == "type" and isinstance(v, list):  # ["string", "null"] -> string, nullable
            types = [t for t in v if t != "null"]
            out["type"] = (types[0] if types else "string").upper()
            if "null" in v:
                out["nullable"] = True
        elif k == "type":
            out["type"] = str(v).upper()
        elif k == "properties":
            out["properties"] = {n: sanitize_schema(p) for n, p in (v or {}).items()}
        elif k == "items":
            out["items"] = sanitize_schema(v)
        else:
            out[k] = v
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)
    return out


def declarations(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        f = spec.get("function", spec)
        d: dict[str, Any] = {"name": f["name"], "description": (f.get("description") or "")[:1000]}
        params = sanitize_schema(f.get("parameters") or {})
        if params.get("properties"):
            d["parameters"] = params
        out.append(d)
    return out


def decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class LiveUI:
    """Live sırasında ajanın UI'ı: izin sorusunu kullanıcının son söylediğine göre cevaplar."""

    def __init__(self, inner=None):
        self.inner = inner
        self.last_user = ""
        self.tools: list[str] = []

    def __getattr__(self, name: str):  # stream_* vb. olayları iç UI'a geçir ya da yut
        fn = getattr(self.inner, name, None) if self.inner is not None else None
        return fn if fn is not None else (lambda *a, **k: None)

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        # Sesli UI'a iletme: o "Bakıyorum" dolgusunu eski ses motoruyla söylüyor, Live de konuşurken çift ses olur.
        self.tools.append(f"{name}({summary[:80]})")

    def tool_end(self, *a) -> None:
        pass

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        return "y" if any(w in self.last_user.lower() for w in YES) else "n"


class AudioOut:
    """24 kHz PCM parçalarını sırayla çalar; clear() ile anında susar (araya girilince)."""

    def __init__(self, rate: int = 24000):
        self.rate = rate
        self.q: queue.Queue[bytes | None] = queue.Queue()
        self._gen = 0
        self.busy_until = 0.0
        self._t = threading.Thread(target=self._run, daemon=True, name="alice-live-out")
        self._t.start()

    def put(self, pcm: bytes) -> None:
        self.q.put(pcm)

    def clear(self) -> None:
        self._gen += 1
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.busy_until = 0.0

    @property
    def playing(self) -> bool:
        return not self.q.empty() or time.monotonic() < self.busy_until

    def close(self) -> None:
        self.clear()
        self.q.put(None)

    PREBUFFER = 0.2  # sn: konuşma başında/boşluktan sonra bu kadar ses birikmeden çalmaya başlama

    def _gather(self, pcm: bytes) -> bytes | None:
        """Sessizlikten sonra ilk parçayı PREBUFFER kadar biriktir: ağdan parçalar düzensiz gelir, beklemeden
        çalınca aradaki boşluklarda ses takılır."""
        if time.monotonic() < self.busy_until:
            return pcm
        need = int(self.rate * self.PREBUFFER) * 2
        deadline = time.monotonic() + self.PREBUFFER + 0.1
        gen = self._gen
        while len(pcm) < need and gen == self._gen:
            try:
                more = self.q.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if more is None:
                self.q.put(None)
                break
            pcm += more
        return pcm

    def _run(self) -> None:
        import sounddevice as sd

        with sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16", latency="high") as out:
            while True:
                pcm = self.q.get()
                if pcm is None:
                    return
                pcm = self._gather(pcm)
                gen = self._gen
                step = self.rate // 20 * 2  # 50 ms
                for i in range(0, len(pcm), step):
                    if gen != self._gen:
                        break
                    piece = pcm[i:i + step]
                    self.busy_until = time.monotonic() + len(piece) / 2 / self.rate + 0.2
                    out.write(piece)


class LiveSession:
    def __init__(self, cfg, agent, log: Callable[[str], None] = lambda s: None,
                 connect: Callable[..., Any] | None = None, out_factory: Callable[[], Any] = AudioOut):
        v = cfg.data.get("voice", {})
        self.cfg = cfg
        self.agent = agent
        self.log = log
        self.key = os.environ.get(cfg.data.get("providers", {}).get("gemini", {}).get("api_key_env", "GEMINI_API_KEY"),
                                  "")
        self.models = list(v.get("live_models") or DEFAULT_MODELS)
        self.voice = str(v.get("live_voice", "Kore"))
        self.idle = float(v.get("live_idle", 20))
        self._connect = connect
        self._out_factory = out_factory
        self.model = ""
        self._pre: dict[str, Any] = {}  # önceden açılan bağlantı: {"thread", "ws", "error"}
        self._pre_lock = threading.Lock()

    @property
    def available(self) -> bool:
        if not self.key:
            return False
        if self._connect is not None:
            return True
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            return False
        return True

    def _setup(self, model: str) -> dict[str, Any]:
        from datetime import datetime

        from alice.voice.assistant import spoken_time

        now = datetime.now()
        system = self.agent.messages[0]["content"] + "\n\n" + LIVE_PROMPT.format(
            now=f"{now:%d.%m.%Y %H:%M} ({spoken_time(now.hour, now.minute)})")
        enabled = self.agent.ctx.state.setdefault("tool_groups", set())
        enabled |= LIVE_GROUPS
        return {"setup": {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}, "languageCode": "tr-TR"}},
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": declarations(self.agent.tool_specs())}],
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }}

    def _open(self):
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        errors = []
        for model in self.models:
            try:
                ws = connect(URL.format(key=self.key), open_timeout=8, max_size=None)
            except Exception as e:  # noqa: BLE001
                raise LiveError(f"bağlanılamadı: {e}") from e
            try:
                ws.send(json.dumps(self._setup(model)))
                msg = decode(ws.recv(timeout=10))
                if "setupComplete" in msg:
                    self.model = model
                    return ws
                errors.append(f"{model}: {str(msg)[:120]}")
            except Exception as e:  # noqa: BLE001 - model yok/kapalı: sıradakini dene
                errors.append(f"{model}: {e}")
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        raise LiveError("; ".join(errors) or "live modeli yok")

    def prefetch(self) -> None:
        """Konuşma biter bitmez bağlantıyı açmaya başla (uyandırma kontrolüyle aynı anda): ~0,8 sn kazanç.
        Uyandırma yoksa discard() ile kapatılır; bu bir model çağrısı değil, sadece bağlantı."""
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {}

            def run() -> None:
                try:
                    pre["ws"] = self._open()
                except Exception as e:  # noqa: BLE001
                    pre["error"] = e

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-pre")
            self._pre = pre
            pre["thread"].start()

    def discard(self) -> None:
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if not pre:
            return

        def close() -> None:
            pre["thread"].join(15)
            ws = pre.get("ws")
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=close, daemon=True).start()

    def _take(self):
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            pre["thread"].join(15)
            if pre.get("ws") is not None:
                return pre["ws"]
            if isinstance(pre.get("error"), LiveError):
                raise pre["error"]
        return self._open()

    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"") -> None:
        """Mikrofon karelerini (16 kHz, 30 ms) modele akıt, cevabı çal, araçları çalıştır. Sessizlikte döner.
        Bu fonksiyon araçları çağıran thread'de çalışır (tarayıcı gibi thread'e bağlı araçlar için)."""
        ws = self._take()
        out = self._out_factory()
        ui = LiveUI(self.agent.ui)
        old_ui, self.agent.ui = self.agent.ui, ui
        calls: queue.Queue[dict[str, Any] | None] = queue.Queue()
        done = threading.Event()
        state = {"active": time.monotonic(), "said": [], "heard": [], "mute": False}
        self.log(f"live açıldı [{self.model}]")

        def touch() -> None:
            state["active"] = time.monotonic()

        def reader() -> None:
            try:
                while not done.is_set():
                    msg = decode(ws.recv())
                    sc = msg.get("serverContent")
                    if sc:
                        if sc.get("interrupted"):
                            out.clear()
                            self.log("live araya girildi (kullanıcı konuştu ya da gürültü)")
                        for part in (sc.get("modelTurn") or {}).get("parts", []):
                            data = (part.get("inlineData") or {}).get("data")
                            if data and not part.get("thought") and not state["mute"]:
                                out.put(base64.b64decode(data))
                                touch()
                        if (t := (sc.get("inputTranscription") or {}).get("text")):
                            state["heard"].append(t)
                            ui.last_user = "".join(state["heard"])[-200:]
                            touch()
                        if (t := (sc.get("outputTranscription") or {}).get("text")):
                            state["said"].append(t)
                            if not state["mute"] and looks_internal("".join(state["said"])):
                                state["mute"] = True  # iç notu okuyor: bu turun kalanını çalma
                                out.clear()
                                self.log(f"live iç not susturuldu: {''.join(state['said'])[-160:]}")
                        if sc.get("turnComplete") or sc.get("interrupted"):
                            self._flush_log(state, ui)
                            state["mute"] = False
                    if msg.get("toolCall"):
                        for fc in msg["toolCall"].get("functionCalls", []):
                            calls.put(fc)
                        touch()
                    if "goAway" in msg:
                        break
            except Exception as e:  # noqa: BLE001 - bağlantı kapandı
                if not done.is_set():
                    self.log(f"live bağlantı bitti: {e}")
            finally:
                done.set()
                calls.put(None)

        def sender() -> None:
            buf = b""
            while not done.is_set() and not stop.is_set():
                try:
                    buf += frames.get(timeout=0.2)
                except queue.Empty:
                    continue
                if len(buf) >= 3200:  # 100 ms
                    try:
                        ws.send(json.dumps({"realtimeInput": {"audio": {
                            "data": base64.b64encode(buf).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
                    except Exception:  # noqa: BLE001
                        done.set()
                        return
                    buf = b""

        threading.Thread(target=reader, daemon=True, name="alice-live-in").start()
        threading.Thread(target=sender, daemon=True, name="alice-live-mic").start()
        try:
            if first_audio:  # "Hey Alice ..." cümlesinin kendisi: yazıya çevirmeden doğrudan modele
                for i in range(0, len(first_audio), 32000):
                    ws.send(json.dumps({"realtimeInput": {"audio": {
                        "data": base64.b64encode(first_audio[i:i + 32000]).decode(),
                        "mimeType": "audio/pcm;rate=16000"}}}))
                # Ardından kısa sessizlik: mikrofon karesi gecikse de sunucu "konuşma bitti"yi görsün.
                ws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(b"\x00\x00" * 9600).decode(), "mimeType": "audio/pcm;rate=16000"}}}))
            if first_text:
                ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": first_text}]}],
                                                      "turnComplete": True}}))
                state["heard"].append(first_text)
                ui.last_user = first_text
            while not done.is_set() and not stop.is_set():
                try:
                    fc = calls.get(timeout=0.3)
                except queue.Empty:
                    fc = None
                if fc is not None:
                    self._run_call(ws, fc)
                    touch()
                    continue
                if out.playing:
                    touch()
                if time.monotonic() - state["active"] > self.idle:
                    break
        finally:
            done.set()
            self._flush_log(state, ui)
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + 15
            while out.playing and time.monotonic() < deadline:  # son cümleyi yarıda kesme
                time.sleep(0.1)
            out.close()
            self.agent.ui = old_ui
            self.log("live kapandı")

    def _run_call(self, ws, fc: dict[str, Any]) -> None:
        call = {"id": fc.get("id") or fc.get("name"), "type": "function",
                "function": {"name": fc.get("name", ""), "arguments": json.dumps(fc.get("args") or {})}}
        try:
            self.agent._enable_groups_for(tool_names=(call["function"]["name"],))
            res: ToolResult = self.agent._execute_calls([call])[0]
        except Exception as e:  # noqa: BLE001
            res = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        text = res.text[:6000]
        if res.is_error and "reddetti" in text:
            text = "onay gerekli: kullanıcıya kısaca sor, 'evet' derse aynı çağrıyı tekrarla."
        elif res.is_error:
            text = "HATA: " + text
        ws.send(json.dumps({"toolResponse": {"functionResponses": [
            {"id": fc.get("id"), "name": fc.get("name"), "response": {"result": text}}]}}))

    def _flush_log(self, state: dict[str, Any], ui: LiveUI) -> None:
        heard, said = "".join(state["heard"]).strip(), "".join(state["said"]).strip()
        if heard or said or ui.tools:
            tools = f" araçlar: {', '.join(ui.tools)}" if ui.tools else ""
            self.log(f"live [{self.model}] duyulan: {heard} | cevap: {said}{tools}")
        state["heard"], state["said"] = [], []
        ui.tools = []
