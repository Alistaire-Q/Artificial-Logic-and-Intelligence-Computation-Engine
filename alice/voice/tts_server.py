"""ALICE yerel ses sunucusu (Chatterbox çok dilli).

Ayrı bir sanal ortamda çalışır (`alice voice install-local` kurar): Chatterbox'ın PyTorch/numpy
sürümleri ALICE'in ortamını bozmasın, model de ekran kartına bir kez yüklenip çekirdek, sesli
asistan ve hızlı bar tarafından paylaşılsın. Bu dosya ALICE paketini içe aktarmaz.

Sadece 127.0.0.1'e bağlanır.
  GET  /health                 -> {"ok": true, "device": "cuda", "loaded": true}
  POST /synth  (JSON)          -> audio/wav
       {"text", "language": "tr", "ref": "C:/.../calm.wav" | "", "exaggeration", "cfg_weight", "temperature",
        "device": "cuda"|"cpu", "verify": true, "whisper_model": "small"}
       verify: üretilen ses yerel Whisper ile dinlenir; cümlenin son kelimesinden sonrası (uydurma, cızırtı)
       kesilir; söylenen yazılandan çok farklıysa bir kez daha üretilip iyisi seçilir.
"""

from __future__ import annotations

import os
import sys

# Bu dosya alice/voice içinden betik olarak çalışıyor: o klasördeki modüller (ör. audio.py) standart
# kütüphaneyle ya da numpy/torch'un içe aktardıklarıyla çakışmasın diye klasörü yoldan çıkar.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402
import wave  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
STATE = {"model": None, "device": None}


def load(device: str):
    import torch
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    if STATE["model"] is not None and STATE["device"] == device:
        return STATE["model"]
    if STATE["model"] is not None:
        # Cihaz değişti (ör. uçuş modunda GPU'yu bırak): eskisini boşalt, yeniden yükle (ağırlıklar diskte).
        STATE["model"] = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    STATE["model"] = ChatterboxMultilingualTTS.from_pretrained(device=device)
    STATE["device"] = device
    return STATE["model"]


WHISPER = {"model": None, "key": None}


def load_whisper(name: str, device: str):
    """OpenAI Whisper (yerel, ücretsiz). Kurulu değilse None: doğrulama atlanır."""
    try:
        import whisper
    except ImportError:
        return None
    key = (name, device)
    if WHISPER["key"] != key:
        WHISPER["model"] = None
        WHISPER["model"] = whisper.load_model(name, device=device)
        WHISPER["key"] = key
    return WHISPER["model"]


def fold(s: str) -> list:
    import re
    import unicodedata

    s = s.lower().replace("ı", "i").replace("İ", "i")
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


def same(a: str, b: str) -> bool:
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:4] == b[:4])


def assess(words: list, text: str) -> tuple:
    """(kalite 0..1, kesilecek saniye ya da None). Kalite: istenen kelimelerin ne kadarı duyuldu,
    fazladan (uydurma) kelime ne kadar var."""
    want = fold(text)
    if not want:
        return 1.0, None
    heard = []
    for w, start, end in words:
        for x in fold(w):
            heard.append((x, start, end))
    matched = {y for y in want if any(same(x, y) for x, _, _ in heard)}
    extra = [x for x, _, _ in heard if not any(same(x, y) for y in want)]
    quality = len(matched) / len(set(want)) - 0.5 * max(0, len(extra) - 1) / len(want)
    seen = set()
    end_at = None
    for x, _start, end in heard:
        if any(same(x, y) for y in want):
            seen.add(x)
        # Son kelime, cümlenin çoğu söylendikten sonra geldiyse orası gerçek son.
        if same(x, want[-1]) and len(seen) >= 0.6 * len(set(want)):
            end_at = end
            break
    return max(0.0, quality), end_at


def transcribe_words(samples, rate: int, name: str, device: str):
    import numpy as np

    model = load_whisper(name, device)
    if model is None:
        return None
    n16 = int(len(samples) * 16000 / rate)
    audio16 = np.interp(np.linspace(0, len(samples) - 1, n16), np.arange(len(samples)), samples).astype("float32")
    res = model.transcribe(audio16, language="tr", word_timestamps=True, fp16=(device == "cuda"), temperature=0.0,
                           condition_on_previous_text=False)
    return [(w.get("word", ""), float(w.get("start", 0)), float(w.get("end", 0)))
            for seg in res.get("segments", []) for w in seg.get("words", []) or []]


def synth(d: dict) -> tuple:
    """(wav baytları, ek başlıklar)."""
    import inspect

    import numpy as np

    text = str(d["text"]).strip()
    if text and text[-1] not in ".!?…":
        text += "."  # noktalamasız biten cümlede model sonunu uydurmaya daha yatkın
    device = d.get("device") or "cuda"
    verify = bool(d.get("verify", True))
    headers = {"X-Alice-Verified": "0"}
    with LOCK:
        model = load(device)
        device = STATE["device"]
        rate = int(getattr(model, "sr", 24000))
        wanted = {"language_id": d.get("language") or "tr"}
        if d.get("ref"):
            wanted["audio_prompt_path"] = d["ref"]
        for k in ("exaggeration", "cfg_weight", "temperature", "repetition_penalty", "min_p", "top_p"):
            if d.get(k) is not None:
                wanted[k] = float(d[k])
        # Chatterbox sürümleri arasında parametreler değişiyor: sadece desteklenenleri ver.
        try:
            params = inspect.signature(model.generate).parameters
            accepts_any = any(p.kind is p.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            params, accepts_any = {}, True
        best = None
        for attempt in range(2 if verify else 1):
            kw = dict(wanted)
            if attempt and "temperature" in kw:
                kw["temperature"] = max(0.3, kw["temperature"] * 0.75)  # ikinci deneme daha temkinli
            kwargs = {k: v for k, v in kw.items() if accepts_any or k in params}
            wav = model.generate(text, **kwargs)
            samples = wav.squeeze().detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav).squeeze()
            samples = samples.astype("float32")
            if not verify:
                best = (samples, None, None)
                break
            try:
                words = transcribe_words(samples, rate, d.get("whisper_model") or "small", device)
            except Exception:  # noqa: BLE001 - doğrulama sesi engellemesin
                traceback.print_exc()
                words = None
            if words is None:
                best = (samples, None, None)
                break
            quality, end_at = assess(words, text)
            if best is None or best[1] is None or quality > best[1]:
                best = (samples, quality, end_at)
            if quality >= 0.85:
                break
        samples, quality, end_at = best
        if quality is not None:
            headers = {"X-Alice-Verified": "1", "X-Alice-Quality": f"{quality:.2f}"}
            if end_at is not None and end_at + 0.15 < len(samples) / rate:
                samples = samples[: int((end_at + 0.15) * rate)]
                headers["X-Alice-Trimmed"] = "1"
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue(), headers


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):  # noqa: N802
        if not self._host_ok() or self.path != "/health":
            return self._send(404, b"{}", "application/json")
        body = {"ok": True, "device": STATE["device"], "loaded": STATE["model"] is not None}
        self._send(200, json.dumps(body).encode(), "application/json")

    def do_POST(self):  # noqa: N802
        if not self._host_ok() or self.path != "/synth":
            return self._send(404, b"{}", "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            if not str(data.get("text", "")).strip():
                return self._send(400, b'{"error": "text bos"}', "application/json")
            wav, headers = synth(data)
            self._send(200, wav, "audio/wav", headers)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8771)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--preload", action="store_true", help="modeli açılışta yükle")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if a.preload:
        def preload():
            with LOCK:
                load(a.device)

        threading.Thread(target=preload, daemon=True).start()
    print(f"ALICE ses sunucusu 127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
