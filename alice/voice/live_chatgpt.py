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

import httpx

from alice.tools.base import ToolResult
from alice.voice.live import AudioOut, LiveError, LiveUI, sanitize_schema

REALTIME_PROMPT = """# Canlı sesli konuşma
Kullanıcıyla sesli, canlı konuşuyorsun. Doğal, sıcak ve kısa konuş (genelde 1-2 cümle), Türkçe.
Liste, kod, uzun yol ya da sayı okuma; gerekirse ekrana koyduğunu söyle.
Sen hızlı bir ses modelisin; bilgi, açıklama, plan, kod, hesap, güncel bir şey ya da ALICE'in araçlarıyla
yapılacak bir iş istenince `think` aracını çağır. Önce tek kısa cümleyle ("Bir bakayım.") haber ver, sonra
`think`in döndürdüğü sonucu kendi cümlelerinle, kısa ve doğal biçimde anlat. Sonucu olduğu gibi okuma.
`think` bir soru sorduysa (güçlü modelin sana ihtiyacı olan bilgi) o soruyu kullanıcıya aynen ilet ve cevabını
bekle; cevabı yine `think` aracına istek olarak ver. Bir iş sürüyorsa aynı konuşmayı sürdür.
Sen hiçbir şey demeden araç çağırırsan kullanıcı kısa bir "Bir saniye." duyar; araç sonucundan sonra bunu
tekrarlama, doğrudan sonucu söyle.
Araçların vermediği sayıları ve durumları asla uydurma; bilmiyorsan söyle.
Mikrofon oyun/video/başkalarının konuşmasını da duyar; sana söylenmemiş, anlamsız ya da Türkçe olmayan
cümlelere cevap verme, sessiz kal. Her zaman Türkçe konuş."""

THINK_TOOL = {
    "type": "function",
    "name": "think",
    "description": ("Bilgi, açıklama, plan, kod, hesap, güncel bilgi ya da ALICE'in araçlarıyla yapılacak işleri "
                    "güçlü modele devret. Sonucu kullanıcıya kısa cümlelerle anlat; soru dönerse kullanıcıya ilet."),
    "parameters": {"type": "object", "properties": {
        "request": {"type": "string", "description": "Kullanıcının isteği; eksiksiz, olduğu gibi"}},
        "required": ["request"]},
}

HUSH_WORDS = ("sus", "sussana", "kes", "yeter", "boşver", "kapat")


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
        self.voice = str(v.get("live_realtime_voice") or "marin")
        self.idle = min(float(v.get("live_idle", 5)), 5)
        self.silence_ms = int(v.get("live_silence_ms", 600))
        self.tools_on = bool(v.get("live_tools", True))
        # Hızlı ses modeli zayıf: bilgi/plan/kod/araç işlerini güçlü modele devreden think aracı.
        self.think = bool(v.get("live_think", True))
        self._think_lock = threading.Lock()
        self.gated = False
        self.hushed = False
        self.last_active = 0.0
        self.single_turn = False
        self.announce: Callable[[str], None] | None = None
        self.on_correction: Callable[[str], None] | None = None
        self._out_factory = out_factory
        self._connect = connect
        self._out = None
        self._ws = None
        self._stop = None
        self._tools_q: queue.Queue | None = None
        self._pre: dict[str, Any] = {}
        self._pre_lock = threading.Lock()
        self._proxy = self._read_proxy()

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
        return (base + "\n\n" + REALTIME_PROMPT)[:12000]

    def _tool_specs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [THINK_TOOL] if self.think else []
        if not self.tools_on:
            return out
        tools = getattr(self.agent, "tools", None)
        if isinstance(tools, dict):
            raw = [t.spec() for name, t in tools.items() if not name.startswith("mcp__") and name != "load_tools"]
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
            params = sanitize_schema(f.get("parameters") or {})
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
                                             "prefix_padding_ms": 300, "silence_duration_ms": self.silence_ms}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": self.voice},
            },
        }
        tools = self._tool_specs()
        if tools:
            session["tools"] = tools
            session["tool_choice"] = "auto"
        return {"expires_after": {"anchor": "created_at", "seconds": 900}, "session": session}

    def _secret(self) -> str:
        if not self._proxy:
            raise LiveError("yerel Claude Code proxy'si (CLIProxyAPI) ayarlı değil")
        try:
            r = httpx.post(f"{self._proxy['base']}/v1/realtime/client_secrets",
                           headers={"Authorization": f"Bearer {self._proxy['key']}"},
                           json=self._session_config(), timeout=20)
            r.raise_for_status()
            value = str(r.json().get("value") or "")
        except Exception as e:  # noqa: BLE001 - Live bağlanamadı: çağıran eski yola döner
            raise LiveError(f"realtime oturumu açılamadı: {e}") from e
        if not value:
            raise LiveError("realtime istemci anahtarı boş döndü")
        return value

    def _ws_url(self) -> str:
        base = (self._proxy or {}).get("base", "http://127.0.0.1:8317")
        ws = base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws}/v1/realtime?model={self.model}"

    # ------------------------------------------------------------------ ön hazırlık
    def prefetch(self, owner: Any = None) -> None:
        """Uyanma anında oturum anahtarını önceden al (bağlantı maliyeti konuşmaya binmesin)."""
        if not self.available:
            return
        with self._pre_lock:
            if self._pre:
                return
            pre: dict[str, Any] = {"owner": owner}

            def run() -> None:
                try:
                    pre["secret"] = self._secret()
                except Exception as e:  # noqa: BLE001
                    pre["error"] = e

            pre["thread"] = threading.Thread(target=run, daemon=True, name="alice-live-rt-pre")
            self._pre = pre
            pre["thread"].start()

    def discard(self, owner: Any = None) -> None:
        with self._pre_lock:
            if owner is not None and self._pre and self._pre.get("owner") not in (None, owner):
                return
            self._pre = {}

    def _take_secret(self) -> str:
        with self._pre_lock:
            pre, self._pre = self._pre, {}
        if pre:
            pre["thread"].join(self.PREFETCH_WAIT)
            if pre.get("secret"):
                return str(pre["secret"])
            if pre.get("error"):
                self.log(f"realtime ön hazırlık başarısız: {pre['error']}")
        return self._secret()

    # ------------------------------------------------------------------ ana akış
    def converse(self, first_text: str, frames: "queue.Queue[bytes]", stop: threading.Event,
                 first_audio: bytes = b"", hint: Callable[[], str | None] | None = None,
                 ended: float | None = None, need_wake: bool = False) -> None:
        """Mikrofon karelerini (16 kHz, 30 ms) modele akıt, cevabı çal, araçları çalıştır. Sessizlikte döner."""
        if hint is not None and not first_text:
            try:
                first_text = hint() or ""
            except Exception:  # noqa: BLE001
                first_text = ""
        secret = self._take_secret()
        if self._connect is not None:
            ws = self._connect(self._ws_url(), secret)
        else:
            from websockets.sync.client import connect

            try:
                ws = connect(self._ws_url(), additional_headers={"Authorization": f"Bearer {secret}"},
                             open_timeout=10, max_size=None)
            except Exception as e:  # noqa: BLE001
                raise LiveError(f"realtime bağlanamadı: {e}") from e
        out = self._out_factory()
        self._ws, self._out, self._stop = ws, out, stop
        ui = LiveUI(self.agent.ui)
        old_ui, self.agent.ui = self.agent.ui, ui
        self.hushed = False
        state = {"active": time.monotonic(), "heard": "", "said": "", "calls": {}, "done": False,
                 "answered": False, "want_response": False}
        self.log(f"realtime oturumu açıldı [{self.model}]")
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
            ws.recv()  # session.created (ya da hata); ayrıntısına gerek yok
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
                    if time.monotonic() - state["active"] > self.idle and not self.playing:
                        self.log("realtime: sessizlik, oturum kapanıyor")
                        break
                    continue
                ev = self._decode(raw)
                kind = ev.get("type", "")
                if kind in ("response.output_audio.delta", "response.audio.delta"):
                    try:
                        out.put(base64.b64decode(ev.get("delta", "")))
                    except Exception:  # noqa: BLE001
                        pass
                    state["answered"] = True
                    touch()
                elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                    state["said"] += ev.get("delta", "")
                    touch()
                elif kind == "response.output_audio_transcript.done" or kind == "response.audio_transcript.done":
                    state["said"] = ev.get("transcript") or state["said"]
                elif kind == "conversation.item.input_audio_transcription.completed":
                    text = str(ev.get("transcript") or "").strip()
                    if text:
                        state["heard"] += " " + text
                        ui.heard(text + " ")
                        self.log(f"realtime duydu: {text[:160]}")
                        if self._is_hush(text):
                            self.hushed = True
                            out.clear()
                            state["done"] = True
                        touch()
                elif kind == "input_audio_buffer.speech_started":
                    out.clear()  # kullanıcı araya girdi: ALICE sussun
                    touch()
                elif kind in ("response.function_call_arguments.done",):
                    state["calls"][str(ev.get("call_id") or "")] = {
                        "id": str(ev.get("call_id") or ""), "type": "function",
                        "function": {"name": str(ev.get("name") or ""), "arguments": str(ev.get("arguments") or "{}")}}
                elif kind == "response.done":
                    calls = [c for c in state["calls"].values() if c["function"]["name"]]
                    state["calls"] = {}
                    if calls:
                        if tool_thread is not None and tool_thread.is_alive():
                            tool_thread.join(5)
                        tool_thread = threading.Thread(target=self._handle_calls, args=(calls, send), daemon=True)
                        tool_thread.start()
                    else:
                        if self.single_turn and state["answered"]:
                            state["done"] = True
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
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                out.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws, self._out = None, None
            self._hud("idle", "")

    # ------------------------------------------------------------------ yardımcılar
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
        try:
            from alice.hud import views

            views.voice_state(state, detail)
        except Exception:  # noqa: BLE001 - HUD yoksa konuşma bozulmasın
            pass

    def _handle_calls(self, calls: list[dict[str, Any]], send: Callable[[dict[str, Any]], None]) -> None:
        """Araçları çalıştır, sonuçları modele geri yaz, cevabı yeniden üret."""
        outputs = []
        for call in calls:
            res = self._run_tool(call)
            text = res.text if isinstance(res, ToolResult) else str(res)
            outputs.append({"type": "function_call_output", "call_id": call["id"],
                            "output": json.dumps({"ok": not getattr(res, "is_error", False),
                                                  "result": text[:4000]}, ensure_ascii=False)})
        try:
            for item in outputs:
                send({"type": "conversation.item.create", "item": item})
            send({"type": "response.create", "response": {"output_modalities": ["audio"]}})
        except Exception as e:  # noqa: BLE001
            self.log(f"realtime: araç sonucu gönderilemedi: {e}")

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
            try:
                reply = str(self.agent.run_turn(text) or "").strip()
            except Exception as e:  # noqa: BLE001 - canlı konuşma düşmesin
                return ToolResult(f"think hatası: {type(e).__name__}: {e}", is_error=True)
            return ToolResult(reply or "Güçlü model boş cevap döndürdü.", is_error=not reply)
        finally:
            self._think_lock.release()

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
