"""Sesli asistan döngüsü: dinle -> yazıya çevir -> ALICE -> konuş.

- Bas-konuş tuşu her zaman çalışır; "Hey Alice" el gerektirmez.
- ALICE cevap verdikten sonra birkaç saniye boyunca "Hey Alice" demeden devam edilebilir.
- ALICE konuşurken araya girersen susar, yarım kalan işi durdurur ve seni dinler.
- Cevap akarken ilk cümle hazır olur olmaz söylenir; araç çalışacaksa önce "hemen bakıyorum" der.
"""

from __future__ import annotations

import queue
import random
import threading
import time
from typing import Any, Callable

from alice.tools.base import ToolResult
from alice.voice.audio import Audio, Microphone
from alice.voice.listen import VAD, PushToTalk, WakeWord, strip_wake, utterances
from alice.voice.speaker import Speaker
from alice.voice.stt import STTError, is_hallucination
from alice.voice.tone import SentenceSplitter

VOICE_PROMPT = """# Voice mode
You are speaking out loud through text-to-speech. The user hears you, they may not be looking at the screen.
- Answer in Turkish, in 1-3 short spoken sentences unless asked for detail. No markdown, lists, tables or emojis.
- Each user message ends with the current date and time in brackets. Use it for time/date questions (say the time exactly as its "söylenişi" part);
  answer simple questions (time, date, small talk, general knowledge) directly without calling tools.
- Long-term memory is already in your system prompt: do not call the memory tool just to read it.
- Weather: call the weather tool once (never run_command/curl for it).
- Never read code, paths or long numbers aloud; say you put it on screen.
- In urgent situations (flight warnings, errors) be clear and brief, no jokes.
- Checklists (checklist tool): read only the current step, then wait; on "tamam", "devam" or "sonraki" call next.
- "Not al" during a flight goes to the kneeboard (kneeboard tool, action note); otherwise to the notes tool."""

def with_clock(text: str) -> str:
    """Sesli asistan saatlerce açık kalır; sistem istemindeki saat eskir. Saati/tarihi her cümleye ekle ki
    "saat kaç" gibi sorular komut çalıştırmadan cevaplansın."""
    from datetime import datetime

    days = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    now = datetime.now()
    return (f"{text}\n\n[şu an: {now:%d.%m.%Y} {days[now.weekday()]}, saat {now:%H:%M}, "
            f"söylenişi: {spoken_time(now.hour, now.minute)}]")


NUMS = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on", "on bir", "on iki"]
TENS = {10: "on", 20: "yirmi"}
ACC = {"bir": "biri", "iki": "ikiyi", "üç": "üçü", "dört": "dördü", "beş": "beşi", "altı": "altıyı", "yedi": "yediyi",
       "sekiz": "sekizi", "dokuz": "dokuzu", "on": "onu"}
DAT = {"bir": "bire", "iki": "ikiye", "üç": "üçe", "dört": "dörde", "beş": "beşe", "altı": "altıya", "yedi": "yediye",
       "sekiz": "sekize", "dokuz": "dokuza", "on": "ona"}


def _num(n: int) -> str:
    return NUMS[n] if n <= 12 else " ".join(w for w in (TENS[n // 10 * 10], NUMS[n % 10]) if w)


def _case(word: str, table: dict[str, str]) -> str:
    head, _, last = word.rpartition(" ")
    return f"{head} {table[last]}".strip()


def spoken_time(hour: int, minute: int) -> str:
    """Model "01:13"ü "bire on üç" diye yanlış okuyordu; saatin Türkçe söylenişini hazır ver."""
    def h12(h: int) -> str:
        return _num(h % 12 or 12)

    if minute == 0:
        return f"saat tam {h12(hour)}"
    if minute == 30:
        return f"saat {h12(hour)} buçuk"
    if minute < 30:
        return f"saat {_case(h12(hour), ACC)} {'çeyrek' if minute == 15 else _num(minute)} geçiyor"
    rest = 60 - minute
    return f"saat {_case(h12(hour + 1), DAT)} {'çeyrek' if rest == 15 else _num(rest)} var"


FILLERS = ["Hemen bakıyorum.", "Bir saniye, bakıyorum.", "Tamam, bakıyorum."]
YES = ("evet", "tamam", "olur", "onay", "yap", "devam", "peki")


class VoiceUI:
    """Ajanın UI'ı: metni iç UI'a (terminal/uygulama) iletir, aynı zamanda seslendirir."""

    def __init__(self, speaker: Speaker, inner=None, ask: Callable[[str], bool] | None = None):
        self.speaker = speaker
        self.inner = inner
        self.ask = ask
        self.splitter = SentenceSplitter()
        self.spoke = False
        self.muted = False  # araya girildi: bu turun geri kalanı söylenmez
        self.models: list[str] = []
        self.tools: list[str] = []

    def _say(self, text: str, emotion: str | None = None) -> None:
        if not self.muted:
            self.spoke = self.speaker.say(text, emotion) or self.spoke

    def mute(self) -> None:
        self.muted = True
        self.speaker.stop()

    def _inner(self, name: str, *args: Any) -> Any:
        fn = getattr(self.inner, name, None) if self.inner is not None else None
        return fn(*args) if fn else None

    def begin_turn(self) -> None:
        self.splitter = SentenceSplitter()
        self.spoke = False
        self.muted = False
        self.models, self.tools = [], []

    def stream_start(self) -> None:
        self._inner("stream_start")

    def stream_delta(self, text: str) -> None:
        self._inner("stream_delta", text)
        for s in self.splitter.feed(text):
            self._say(s)

    def thinking_delta(self, text: str) -> None:
        self._inner("thinking_delta", text)

    def stream_reset(self) -> None:
        self._inner("stream_reset")
        self.speaker.stop()
        self.splitter = SentenceSplitter()

    def stream_end(self, text: str, source: str) -> None:
        self._inner("stream_end", text, source)
        self.models.append(source)
        rest = self.splitter.flush()
        if not self.spoke and not rest and text:
            # Akış yoktu (stream kapalı ya da yedek model): metnin tamamını söyle.
            rest = SentenceSplitter().feed(text + "\n") or [text]
        for s in rest:
            self._say(s)
        self.splitter = SentenceSplitter()

    def tool_start(self, call_id: str, name: str, summary: str) -> None:
        self._inner("tool_start", call_id, name, summary)
        self.tools.append(f"{name}({summary[:80]})")
        if not self.spoke and not self.speaker.speaking:
            self._say(random.choice(FILLERS), "calm")

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        self._inner("tool_end", call_id, name, result)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        if self.inner is not None and self.ask is None:
            return self.inner.ask_permission(name, summary, risk)
        if self.ask is None:
            return "n"
        return "y" if self.ask(f"{summary[:120]} için onay veriyor musun?") else "n"

    def info(self, msg: str) -> None:
        self._inner("info", msg)


def voice_log(line: str) -> None:
    """~/.alice/voice.log: ne duyuldu, ne cevap verildi (yanlış anlama sorunlarını görmek için)."""
    from datetime import datetime

    from alice.config import alice_home

    try:
        p = alice_home() / "voice.log"
        if p.exists() and p.stat().st_size > 2_000_000:
            p.replace(p.with_suffix(".log.1"))
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def trim_history(messages: list[dict[str, Any]], keep_turns: int) -> list[dict[str, Any]]:
    """Sistem istemi + son keep_turns kullanıcı turu (araç çağrıları bölünmeden). Sesli asistan saatlerce açık
    kalıyor; tüm geçmiş her istekte gidince eski konulara sapıyor ve küçük bağlamlı hızlı modellere sığmıyor."""
    starts = [i for i, m in enumerate(messages) if i and m.get("role") == "user" and isinstance(m.get("content"), str)
              and not m.get("_image")]
    if len(starts) <= keep_turns:
        return messages
    return messages[:1] + messages[starts[-keep_turns]:]


class VoiceAssistant:
    def __init__(self, agent, speaker: Speaker, stt, mic_factory: Callable[[], Microphone] = Microphone,
                 ptt_key: str = "", wake_model: str = "", wake_phrase: bool = True, barge_in: bool = True,
                 follow_up: float = 8.0, emit: Callable[[str], None] = lambda s: None,
                 keep_turns: int = 4, idle_reset: float = 300.0):
        self.agent = agent
        self.keep_turns = keep_turns
        self.idle_reset = idle_reset  # bu kadar saniye sessizlikten sonra yeni sohbet
        self._last_turn = 0.0
        self.speaker = speaker
        self.stt = stt
        self.mic_factory = mic_factory
        self.ptt_key = ptt_key
        self.wake_model = wake_model
        self.wake_phrase = wake_phrase
        self.barge_in = barge_in
        self.follow_up = follow_up
        self.emit = emit
        self.ui: VoiceUI = agent.ui if isinstance(agent.ui, VoiceUI) else VoiceUI(speaker, agent.ui)
        agent.ui = self.ui
        self.vad = VAD()
        self.stop_event = threading.Event()
        self.armed_until = 0.0      # bu ana kadar "Hey Alice" gerekmez
        self._jobs: queue.Queue = queue.Queue()
        self._answers: queue.Queue = queue.Queue()
        self._awaiting_answer = False
        self._busy = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="alice-voice-turns").start()

    # ---------------------------------------------------------------- olaylar
    def _interrupt(self) -> None:
        if self.speaker.speaking or self._busy.is_set():
            if self._awaiting_answer:
                self.speaker.stop()
                return
            self.ui.mute()
            if self._busy.is_set():
                self.agent.stop()
            self.emit("araya girildi, susuyorum")

    def _on_speech_start(self) -> None:
        if self.barge_in and self.speaker.speaking:
            self._interrupt()

    def ask_yes_no(self, question: str, timeout: float = 10) -> bool:
        """Sesli onay: soruyu söyle, sıradaki konuşmayı cevap say."""
        self.speaker.say(question, "serious")
        self.speaker.wait(15)
        self._awaiting_answer = True
        self.armed_until = time.monotonic() + timeout
        try:
            ans = self._answers.get(timeout=timeout)
        except queue.Empty:
            return False
        finally:
            self._awaiting_answer = False
        return any(w in ans.lower() for w in YES)

    def handle_text(self, text: str, direct: bool) -> None:
        """Yazıya çevrilmiş konuşmayı işle (test edilebilir çekirdek)."""
        text = text.strip()
        if not text:
            return
        if self._awaiting_answer:
            self._answers.put(text)
            return
        # Uyandırma kelimesi varsa her durumda at; yoksa sadece bas-konuş / takip penceresinde geçerli.
        rest = strip_wake(text)
        armed = direct or time.monotonic() <= self.armed_until
        if rest is not None and (armed or self.wake_phrase):
            text = rest
        elif not armed:
            return
        if not text:
            self.speaker.say("Efendim?", "calm")
            self.armed_until = time.monotonic() + self.follow_up
            return
        if self._busy.is_set():
            self.ui.mute()
            self.agent.stop()
        self._jobs.put(text)

    def handle_audio(self, audio: Audio, direct: bool) -> None:
        if audio.seconds < 0.3:
            return
        gate = getattr(self.stt, "wake_gate", None)
        if not direct and not self._awaiting_answer and gate is not None and self.wake_phrase:
            head = gate.heard(audio)
            if head is not None:
                if strip_wake(head) is None:
                    return  # uyandırma yok: sesi buluta hiç gönderme
                direct = True  # yerel model "Hey Alice" duydu; bulut yazımı farklı olsa da kabul et
        try:
            text = self.stt.transcribe(audio)
        except STTError as e:
            self.emit(f"anlaşılamadı: {e}")
            return
        self.emit(f"duyulan: {text}")
        if is_hallucination(text):
            voice_log(f"duyulan (gürültü sayıldı) [{getattr(self.stt, 'last', '')}]: {text}")
            return
        voice_log(f"duyulan [{getattr(self.stt, 'last', '')}]{' (doğrudan)' if direct else ''}: {text}")
        self.handle_text(text, direct)

    def _worker(self) -> None:
        while True:
            text = self._jobs.get()
            self._busy.set()
            now = time.monotonic()
            msgs = getattr(self.agent, "messages", None)
            if isinstance(msgs, list) and msgs:
                if self._last_turn and now - self._last_turn > self.idle_reset:
                    msgs[:] = msgs[:1]  # uzun sessizlik: yeni sohbet
                else:
                    msgs[:] = trim_history(msgs, self.keep_turns)
            reply = ""
            try:
                self.ui.begin_turn()
                reply = self.agent.run_turn(with_clock(text)) or ""
            except Exception as e:
                self.speaker.say("Bir sorun çıktı, ekrana yazdım.", "serious")
                self.emit(f"hata: {e}")
                reply = f"HATA {e}"
            finally:
                self._busy.clear()
                self._last_turn = time.monotonic()
                tools = f" araçlar: {', '.join(self.ui.tools)}" if self.ui.tools else ""
                voice_log(f"cevap [{', '.join(dict.fromkeys(self.ui.models))}]{tools}: {reply}")
            self.speaker.wait(120)
            self.armed_until = time.monotonic() + self.follow_up

    # ------------------------------------------------------------------ döngü
    def run(self) -> None:
        self.ui.ask = self.ask_yes_no
        ptt = PushToTalk(self.ptt_key) if self.ptt_key else None
        wake = WakeWord(self.wake_model) if self.wake_model else None
        self.wake_phrase = self.wake_phrase and wake is None
        with self.mic_factory() as mic:
            def frames():
                while not self.stop_event.is_set():
                    if ptt is not None and ptt.pressed.is_set():
                        self._interrupt()
                        audio = ptt.record(mic)
                        threading.Thread(target=self.handle_audio, args=(audio, True), daemon=True).start()
                        continue
                    frame = mic.read()
                    if wake is not None and wake.detect(frame):
                        self._interrupt()
                        self.armed_until = time.monotonic() + self.follow_up
                        self.emit("uyandım")
                    # ALICE konuşurken kendi sesini konuşma sanmasın.
                    self.vad.boost = 2.5 if self.speaker.speaking else 1.0
                    yield frame

            for audio in utterances(frames(), self.vad, stop=self.stop_event.is_set,
                                    on_start=self._on_speech_start):
                direct = time.monotonic() <= self.armed_until
                threading.Thread(target=self.handle_audio, args=(audio, direct), daemon=True).start()
        if ptt is not None:
            ptt.stop()

    def stop(self) -> None:
        self.stop_event.set()
        self.speaker.stop()


def build_voice_agent(cfg, inner_ui=None, speaker: Speaker | None = None, cwd=None):
    """Sesli konuşma için ajan: normal araçlar + ses talimatı."""
    from pathlib import Path

    from alice.agent import Agent
    from alice.decider import Decider
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import ToolContext, all_tools
    from alice.voice.tts import make_tts

    router = Router(cfg)
    decider = Decider.from_config(cfg, router)
    ctx = ToolContext(cwd=cwd or Path.home(), config=cfg, router=router, decider=decider)
    if speaker is None:
        tts = make_tts(cfg)
        tts.warm_up()
        speaker = Speaker(tts)
    ui = VoiceUI(speaker, inner_ui)
    agent = Agent(cfg, router, all_tools(), ctx, Permissions(cfg.agent.get("permission_mode", "auto")), ui, decider)
    agent.messages[0]["content"] += "\n\n" + VOICE_PROMPT
    if "voice" in cfg.roles:
        agent.role = "voice"
    return agent, speaker, ctx
