"""ALICE yerel ses sunucusu (Chatterbox çok dilli).

Ayrı bir sanal ortamda çalışır (`alice voice install-local` kurar): Chatterbox'ın PyTorch/numpy
sürümleri ALICE'in ortamını bozmasın, model de ekran kartına bir kez yüklenip çekirdek, sesli
asistan ve hızlı bar tarafından paylaşılsın. Bu dosya ALICE paketini içe aktarmaz.

Sadece 127.0.0.1'e bağlanır.
  GET  /health                 -> {"ok": true, "device": "cuda", "loaded": true}
  POST /synth  (JSON)          -> audio/wav
       {"text", "language": "tr", "ref": "C:/.../calm.wav" | "", "exaggeration", "cfg_weight", "device": "cuda"|"cpu"}
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import traceback
import wave
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


def synth(d: dict) -> bytes:
    import numpy as np

    with LOCK:
        model = load(d.get("device") or "cuda")
        kwargs = {"language_id": d.get("language") or "tr"}
        if d.get("ref"):
            kwargs["audio_prompt_path"] = d["ref"]
        for k in ("exaggeration", "cfg_weight"):
            if d.get(k) is not None:
                kwargs[k] = float(d[k])
        wav = model.generate(str(d["text"]), **kwargs)
        samples = wav.squeeze().detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav).squeeze()
        rate = int(getattr(model, "sr", 24000))
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
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
            self._send(200, synth(data), "audio/wav")
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
