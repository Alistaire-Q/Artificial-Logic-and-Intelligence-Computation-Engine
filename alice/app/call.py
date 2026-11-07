"""Uygulama içi arama: telefondaki ALICE uygulamasıyla sesli görüşme.

Kullanıcı ALICE'i arar ya da ALICE kullanıcıyı arar (phone_call aracı: "yarım saat sonra beni ara", bir hatırlatma,
önemli bir uyarı). Görüşme kendi sohbetinde sürer, ALICE'in bütün araçları açıktır.

Canlı yol (varsayılan, bilgisayardaki sesli asistanla aynı): telefon mikrofonu 16 kHz PCM parçalarıyla
POST /api/call/mic ile gelir, ChatGPT Realtime oturumuna akar; ALICE'in sesi parça parça call_pcm olayıyla telefona
gider ve orada WebAudio ile çalınır. Gecikme ~1-2 sn (eski yol ~10 sn). Realtime yoksa eski yol:

Eski yol (yarı çift yönlü):
  telefon: konuşma tanıma (cihazda, canlı yazı)  ->  POST /api/call/say {text}
  bilgisayar: ajan turu; cevap akarken cümle cümle sese çevrilir (TTS)  ->  olay call_audio {n, text}
  telefon: GET /api/call/audio?n= ile çalar, bitince yeniden dinler. ALICE konuşurken dokunulursa susar, dinler.
Ses üretilemezse (anahtar/kota yok) olay sadece metinle gider, telefon kendi sesiyle okur.
"""

from __future__ import annotations

import base64
import queue
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

RING_SECONDS = 45.0
END_MARK = "[kapat]"
GREETINGS = ("Efendim?", "Alo, söyle.", "Söyle bakalım, dinliyorum.")
CALL_PROMPT = """# Telefon görüşmesi
Şu an kullanıcıyla ALICE uygulaması üzerinden telefonda konuşuyorsun; söylediklerin sesli okunuyor.
- Kısa ve doğal konuş: genelde 1-2 cümle. Liste, kod, bağlantı, tablo, emoji yok.
- Bir iş istenirse ilgili aracı hemen çağır. Sürecek bir işse önce tek kısa cümleyle ne yaptığını söyle.
- Kullanıcı görüşmeyi bitirmek isterse ("kapat", "görüşürüz", "hoşça kal", "tamam bu kadar") kısaca vedalaş ve
  cevabının en sonuna [kapat] yaz.
- Sen aradıysan neden aradığını ilk cümlende söyledin; kullanıcının cevabına göre devam et."""
LIVE_CALL_PROMPT = """# Telefon görüşmesi
Şu an kullanıcıyla ALICE uygulaması üzerinden telefonda, canlı sesle konuşuyorsun.
- Kısa ve doğal konuş: genelde 1-2 cümle. Liste, kod, bağlantı okuma.
- Bir iş istenirse ilgili aracı hemen çağır; sürecekse önce tek kısa cümleyle ne yaptığını söyle.
- Kullanıcı görüşmeyi bitirmek isterse kısaca vedalaş; görüşmeyi uygulama kapatır."""
PCM_FRAME = 960  # 16 kHz PCM16, 30 ms: sesli asistanın mikrofon karesiyle aynı
_BYE = re.compile(r"^\s*(kapat(ıyorum|abilirsin)?|görüşürüz|hoşça ?kal|bay ?bay|güle ?güle|tamam,? (bu kadar|kapat))\W*$",
                  re.I)

_current: "CallManager | None" = None


def current() -> "CallManager | None":
    """Bu süreçte telefon köprüsünü sunan arama yöneticisi (phone_call aracı bunu kullanır)."""
    return _current


def register(mgr: "CallManager | None") -> None:
    global _current
    _current = mgr


@dataclass
class Call:
    id: str
    outbound: bool  # ALICE aradı
    reason: str = ""
    opening: str = ""
    state: str = "ringing"  # ringing | active | ended
    end_reason: str = ""
    started: float = field(default_factory=time.time)
    answered: float = 0.0
    turn: int = 0
    seg: int = 0
    audio: dict[int, bytes] = field(default_factory=dict)
    lines: list[dict[str, str]] = field(default_factory=list)
    chat: Any = None
    hang_after_turn: bool = False
    live: bool = False
    rt: Any = None  # RealtimeSession (canlı yol)
    mic: "queue.Queue[bytes]" = field(default_factory=lambda: queue.Queue(maxsize=400))
    stop: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "outbound": self.outbound, "reason": self.reason,
                "started": self.started, "answered": self.answered, "end_reason": self.end_reason,
                "turn": self.turn, "lines": self.lines[-30:], "sid": self.chat.sid if self.chat is not None else "",
                "live": self.live}


class CallManager:
    def __init__(self, app: Any, tts_factory: Callable[[], Any] | None = None,
                 publish: Callable[[dict[str, Any]], None] | None = None,
                 live_factory: Callable[[Any, Any], Any] | None = None):
        self.app = app
        self.publish = publish or app.bus.publish
        self._tts_factory = tts_factory
        self._tts: Any = None
        self._live_factory = live_factory
        self.call: Call | None = None
        self._lock = threading.Lock()
        self._speech: "queue.Queue[tuple[Call, int, int, str] | None]" = queue.Queue()
        threading.Thread(target=self._speaker, daemon=True, name="alice-call-tts").start()

    # ------------------------------------------------------------------------------------ yardımcılar
    def _get(self, cid: str) -> Call:
        c = self.call
        if c is None or c.id != cid:
            raise KeyError("böyle bir görüşme yok")
        return c

    def active(self) -> Call | None:
        c = self.call
        return c if c is not None and c.state != "ended" else None

    def _state(self, c: Call) -> None:
        self.publish({"type": "call_state", **c.view()})

    def _new_chat(self, c: Call, note: str) -> None:
        from alice.app.server import Chat
        from alice.voice.assistant import VOICE_PROMPT

        chat = Chat(self.app, self.app.ctx.cwd, self.app.agent.permissions.mode)
        prompt = LIVE_CALL_PROMPT if c.live else CALL_PROMPT
        chat.agent.messages[0]["content"] += "\n\n" + VOICE_PROMPT + "\n\n" + prompt
        if "voice" in self.app.cfg.roles:
            chat.agent.role = "voice"
        splitter_box: dict[str, Any] = {}
        orig_delta, orig_reset = chat.ui.stream_delta, chat.ui.stream_reset

        def delta(text: str) -> None:
            orig_delta(text)
            sp = splitter_box.get("s")
            if sp is not None:
                for piece in sp.feed(text):
                    self._say(c, piece)

        def reset() -> None:
            orig_reset()
            sp = splitter_box.get("s")
            if sp is not None:
                sp.buf = ""  # model değişti, yarım cümle atılır (söylenen söylendi)

        chat.ui.stream_delta = delta
        chat.ui.stream_reset = reset
        chat._call_splitter = splitter_box  # type: ignore[attr-defined]
        chat.agent.messages.append({"role": "user", "content": note})
        c.chat = chat

    def _speech_engine(self) -> Any:
        if self._tts is None:
            if self._tts_factory is not None:
                self._tts = self._tts_factory()
            else:
                from alice.voice.tts import make_tts

                self._tts = make_tts(self.app.cfg)
        return self._tts

    def _say(self, c: Call, text: str, turn: int | None = None) -> None:
        """Cümleyi sese çevirme sırasına koy (sıra korunur, ilk cümle hazır olur olmaz çalınır)."""
        text = text.replace(END_MARK, "").strip()
        if not text or c.state == "ended":
            return
        c.seg += 1
        self._speech.put((c, c.seg, c.turn if turn is None else turn, text))

    def _speaker(self) -> None:
        while True:
            item = self._speech.get()
            if item is None:
                return
            c, n, turn, text = item
            if c.state == "ended" or turn < c.turn:
                continue  # kullanıcı araya girdi ya da kapattı: eski cümleler okunmaz
            wav = None
            try:
                wav = self._speech_engine().synth(text, "calm").to_wav()
            except Exception:  # noqa: BLE001 - ses yoksa telefon metni kendi sesiyle okur
                wav = None
            if wav:
                c.audio[n] = wav
                for old in [k for k in c.audio if k < n - 40]:
                    c.audio.pop(old, None)
            self.publish({"type": "call_audio", "id": c.id, "n": n, "turn": turn, "text": text, "audio": bool(wav)})

    # ------------------------------------------------------------------------------------- akış
    def ring(self, reason: str, message: str) -> dict[str, Any]:
        """ALICE kullanıcıyı arar: telefonda gelen arama ekranı açılır. Açılmazsa cevapsız arama olarak kalır."""
        with self._lock:
            if self.active():
                return {"ok": False, "error": "zaten bir görüşme var"}
            c = Call(id=secrets.token_hex(6), outbound=True, reason=reason.strip()[:120],
                     opening=message.strip() or reason.strip())
            self.call = c
        self.publish({"type": "call", "id": c.id, "reason": c.reason, "text": c.opening, "from": "alice"})
        self._state(c)

        def timeout() -> None:
            time.sleep(RING_SECONDS)
            if c.state == "ringing":
                self._end(c, "missed")
                self.publish({"type": "info", "text": f"ALICE seni aradı, açmadın: {c.reason or c.opening}"})

        threading.Thread(target=timeout, daemon=True).start()
        return {"ok": True, "id": c.id}

    def start(self) -> dict[str, Any]:
        """Kullanıcı ALICE'i arar: hemen açılır, ALICE kısa bir selamla karşılar."""
        with self._lock:
            if self.active():
                old = self.call
                assert old is not None
                if old.outbound and old.state == "ringing":  # aynı anda ikisi de aradı: gelen aramayı aç
                    return self.answer(old.id)
                return {"ok": False, "error": "zaten bir görüşme var"}
            c = Call(id=secrets.token_hex(6), outbound=False, state="active", answered=time.time())
            self.call = c
        c.live = self._live_ok()
        if c.live:
            self._new_chat(c, "[Kullanıcı seni ALICE uygulamasından aradı, telefonu açtın.]")
            self._state(c)
            self._run_live(c, "[Kullanıcı seni ALICE uygulamasından aradı, telefonu açtın. Kısa ve doğal bir selam ver: "
                              "\"Efendim?\" ya da \"Alo, söyle\" gibi.]")
            return {"ok": True, **c.view()}
        greet = random.choice(GREETINGS)
        self._new_chat(c, "[Kullanıcı seni ALICE uygulamasından aradı, telefonu açtın.]")
        c.chat.agent.messages.append({"role": "assistant", "content": greet})
        c.lines.append({"who": "alice", "text": greet})
        self._state(c)
        self._say(c, greet)
        return {"ok": True, **c.view()}

    def answer(self, cid: str) -> dict[str, Any]:
        c = self._get(cid)
        if c.state != "ringing":
            return {"ok": c.state == "active", **c.view()}
        c.state, c.answered = "active", time.time()
        c.live = self._live_ok()
        note = f"[Kullanıcıyı telefondan aradın ({c.reason or 'neden belirtilmedi'}). Kullanıcı açtı.]"
        if c.live:
            self._new_chat(c, note)
            c.lines.append({"who": "alice", "text": c.opening})
            self._state(c)
            self._run_live(c, f"{note} İlk cümlen tam olarak şu olsun, sonra kullanıcıyı dinle: {c.opening}")
            return {"ok": True, **c.view()}
        self._new_chat(c, note)
        c.chat.agent.messages.append({"role": "assistant", "content": c.opening})
        c.lines.append({"who": "alice", "text": c.opening})
        self._state(c)
        self._say(c, c.opening)
        return {"ok": True, **c.view()}

    def decline(self, cid: str) -> dict[str, Any]:
        c = self._get(cid)
        if c.state == "ringing":
            self._end(c, "declined")
        return {"ok": True}

    def say(self, cid: str, text: str) -> dict[str, Any]:
        """Kullanıcının söylediği: ALICE cevabını cümle cümle seslendirir. ALICE çalışırken gelirse (araya girme)
        önceki iş durdurulur, eski cümleler okunmaz."""
        from alice.voice.assistant import with_clock
        from alice.voice.tone import SentenceSplitter

        c = self._get(cid)
        text = text.strip()
        if c.state != "active":
            return {"ok": False, "error": "görüşme açık değil"}
        if not text:
            return {"ok": False, "error": "boş"}
        if c.live:  # aramada "Yaz" ya da telefonun kendi konuşma tanıması (mikrofon açılamadıysa)
            if c.rt is None or not c.rt.inject(text):
                return {"ok": False, "error": "ALICE henüz hazır değil, bir saniye sonra tekrar dene"}
            c.lines.append({"who": "user", "text": text})
            self.publish({"type": "call_text", "id": c.id, "who": "user", "text": text})
            return {"ok": True, "turn": c.turn}
        chat = c.chat
        if chat.busy:
            chat.agent.stop()
        c.turn += 1
        turn = c.turn
        c.lines.append({"who": "user", "text": text})
        if _BYE.match(text):  # açık veda: modele gitmeden kapat
            bye = random.choice(("Tamam, görüşürüz.", "Görüşürüz.", "Tamamdır, kapatıyorum."))
            chat.agent.messages += [{"role": "user", "content": text}, {"role": "assistant", "content": bye}]
            c.lines.append({"who": "alice", "text": bye})
            c.hang_after_turn = True
            self._say(c, bye, turn)
            self.publish({"type": "call_turn_done", "id": c.id, "turn": turn, "end": True})
            return {"ok": True, "turn": turn}
        chat.queued = True

        def run() -> None:
            box = chat._call_splitter
            box["s"] = SentenceSplitter()
            reply = ""
            try:
                reply = chat.agent.run_turn(with_clock(text)) or ""
            except Exception as e:  # noqa: BLE001 - görüşme düşmesin
                reply = ""
                self._say(c, "Bir sorun çıktı, tekrar söyler misin?", turn)
                self.publish({"type": "info", "text": f"arama: {e}"})
            finally:
                chat.queued = False
            sp = box.pop("s", None)
            if turn != c.turn or c.state == "ended":
                return
            for piece in sp.flush() if sp else []:
                self._say(c, piece, turn)
            end = END_MARK in reply
            clean = reply.replace(END_MARK, "").strip()
            if clean:
                c.lines.append({"who": "alice", "text": clean})
            c.hang_after_turn = end
            try:
                chat.save()
            except OSError:
                pass
            self.publish({"type": "call_turn_done", "id": c.id, "turn": turn, "end": end})

        chat.run(run)
        return {"ok": True, "turn": turn}

    def hangup(self, cid: str, why: str = "hangup") -> dict[str, Any]:
        c = self._get(cid)
        if c.state != "ended":
            self._end(c, why)
        return {"ok": True}

    def _end(self, c: Call, why: str) -> None:
        c.state, c.end_reason = "ended", why
        c.stop.set()
        chat = c.chat
        if chat is not None:
            if chat.busy:
                chat.agent.stop()
            try:
                chat.save()
            except OSError:
                pass
            chat.close()
        self.publish({"type": "call_end", "id": c.id, "reason": why, "outbound": c.outbound,
                      "text": c.reason or c.opening})
        self._state(c)

    # ------------------------------------------------------------------------------------- canlı yol
    def _live_ok(self) -> bool:
        if self._live_factory is not None:
            return True
        if not self.app.cfg.data.get("voice", {}).get("call_live", True):
            return False
        try:
            from alice.voice.live_chatgpt import RealtimeSession

            return bool(RealtimeSession(self.app.cfg, None).available)
        except Exception:  # noqa: BLE001
            return False

    def _make_session(self, c: Call) -> Any:
        mgr = self

        class PhoneOut:
            """Realtime sesini telefona akıtır. Çalma süresi buradan tahmin edilir (24 kHz PCM16 = 48 000 B/sn)."""

            def __init__(self) -> None:
                self.until = 0.0

            @property
            def playing(self) -> bool:
                return time.monotonic() < self.until

            def put(self, pcm: bytes) -> None:
                if not pcm or c.state == "ended":
                    return
                self.until = max(time.monotonic(), self.until) + len(pcm) / 48000.0
                mgr.publish({"type": "call_pcm", "id": c.id, "pcm": base64.b64encode(pcm).decode()})

            def clear(self) -> None:
                self.until = 0.0
                mgr.publish({"type": "call_pcm_clear", "id": c.id})

            def drain(self, timeout: float = 60.0, stopped: Callable[[], bool] = lambda: False) -> None:
                end = time.monotonic() + timeout
                while self.playing and time.monotonic() < end and not stopped():
                    time.sleep(0.05)

            def close(self) -> None:
                pass

        if self._live_factory is not None:
            rt = self._live_factory(c.chat.agent, PhoneOut)
        else:
            from alice.voice.live_chatgpt import RealtimeSession

            from alice.voice.assistant import voice_log

            rt = RealtimeSession(self.app.cfg, c.chat.agent, log=lambda line: voice_log(f"arama: {line}"),
                                 out_factory=PhoneOut)
        rt.idle = 3600.0  # görüşmeyi sessizlik değil kullanıcı kapatır
        rt.hud_on = False
        rt.prewarm = False
        # Telefonun yankı engelleyicisi ALICE'in sesini mikrofondan siler: konuşurken araya girilebilir.
        rt.echo_guard = bool(self.app.cfg.data.get("voice", {}).get("call_echo_guard", False))

        def heard(text: str) -> None:
            c.lines.append({"who": "user", "text": text})
            self.publish({"type": "call_text", "id": c.id, "who": "user", "text": text})
            if _BYE.match(text):
                c.hang_after_turn = True

        def said(text: str) -> None:
            c.lines.append({"who": "alice", "text": text})
            self.publish({"type": "call_text", "id": c.id, "who": "alice", "text": text})
            if c.hang_after_turn:  # veda edildi: ALICE'in cevabı çalınınca kapat
                out = rt._out

                def later() -> None:
                    if out is not None:
                        out.drain(15.0)
                    time.sleep(0.6)
                    if c.state != "ended":
                        self._end(c, "hangup")

                threading.Thread(target=later, daemon=True).start()

        rt.on_heard, rt.on_said = heard, said
        return rt

    def _run_live(self, c: Call, opening: str) -> None:
        c.rt = self._make_session(c)

        def run() -> None:
            first, fails = opening, 0
            while c.state == "active" and not c.stop.is_set():
                try:
                    c.rt.converse(first, c.mic, c.stop)
                    first, fails = "", 0
                except Exception as e:  # noqa: BLE001 - bağlantı koptu: yeniden bağlan, olmazsa eski yola dön
                    fails += 1
                    self.publish({"type": "info", "text": f"arama: {e}"})
                    low = str(e).lower()
                    if fails >= 3 or "quota" in low or "limit" in low:
                        self._fallback(c)
                        return
                    time.sleep(0.5)
            try:
                c.chat.save()
            except (OSError, AttributeError):
                pass

        threading.Thread(target=run, daemon=True, name="alice-call-live").start()

    def _fallback(self, c: Call) -> None:
        """Canlı ses çalışmıyor: görüşme düşmesin, telefon konuşma tanıma + ses dosyası yoluna geçer."""
        if c.state != "active":
            return
        c.live, c.rt = False, None
        msg = c.chat.agent.messages[0]
        msg["content"] = str(msg["content"]).replace(LIVE_CALL_PROMPT, CALL_PROMPT)
        self._state(c)
        self._say(c, "Canlı bağlantı koptu, normal görüşmeye geçtim. Söyle.")
        self.publish({"type": "call_turn_done", "id": c.id, "turn": c.turn, "end": False})

    def mic(self, cid: str, pcm: bytes) -> dict[str, Any]:
        """Telefondan gelen mikrofon parçası (16 kHz PCM16)."""
        c = self._get(cid)
        if c.state != "active" or not c.live:
            return {"ok": False, "live": c.live}
        for i in range(0, len(pcm) - len(pcm) % 2, PCM_FRAME):
            try:
                c.mic.put_nowait(pcm[i:i + PCM_FRAME])
            except queue.Full:  # oturum yeniden bağlanıyor: eski ses birikmesin
                try:
                    c.mic.get_nowait()
                except queue.Empty:
                    pass
        return {"ok": True}

    def interrupt(self, cid: str) -> dict[str, Any]:
        c = self._get(cid)
        if c.rt is not None:
            c.rt.interrupt()
        return {"ok": True}

    def audio(self, cid: str, n: int) -> bytes | None:
        c = self.call
        if c is None or c.id != cid:
            return None
        return c.audio.get(n)

    def view(self) -> dict[str, Any]:
        c = self.call
        return c.view() if c is not None else {}
