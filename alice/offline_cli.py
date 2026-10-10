"""alice offline: tam çevrimdışı mod.

  alice offline                 durum: internet, yerel model, yerel ses, konuşma tanıma
  alice offline install         bir kez: Ollama + yerel model (alice-local) + Piper Türkçe ses + faster-whisper
  alice offline test            internet varken bile yerel yolu dene: yerel model cevaplar, Piper söyler
  alice offline on|off|auto     on: hep yerel, off: hiç geçme, auto: internet yoksa kendiliğinden (varsayılan)
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from alice import offline
from alice.config import load_config, set_config_value

console = Console(highlight=False)


def offline_cmd(argv: list[str]) -> int:
    cfg = load_config()
    sub = argv[0] if argv else "status"
    if sub in ("on", "off", "auto"):
        set_config_value("offline", "mode", sub)
        console.print({"on": "[green]çevrimdışı mod açık:[/] ALICE hep yerel model ve yerel sesle çalışır.",
                       "off": "[yellow]çevrimdışı mod kapalı:[/] internet gitse de yerele geçmez.",
                       "auto": "[green]otomatik:[/] internet yokken yerele geçer, gelince buluta döner."}[sub])
        console.print("[dim]Çalışan çekirdek bunu 10 sn içinde görür, yeniden başlatmak gerekmez.[/]")
        return 0
    fn = {"status": _status, "install": _install, "test": _test}.get(sub)
    if fn is None:
        console.print(__doc__)
        return 0 if sub in ("-h", "--help", "help") else 2
    return fn(cfg, argv[1:])


def _ollama_base(cfg) -> str:
    return str(cfg.data.get("providers", {}).get("ollama", {}).get("base_url", "http://localhost:11434/v1"))


def _ok(flag: bool, yes: str = "var", no: str = "yok") -> str:
    return f"[green]{yes}[/]" if flag else f"[red]{no}[/]"


def _status(cfg, argv) -> int:
    import importlib.util

    from alice.voice.tts import piper_files

    o = offline.settings(cfg.data)
    net = offline.probe_internet()
    mode = offline.NetState(mode=lambda: str(cfg.data.get("offline", {}).get("mode", "auto"))).forced()
    base = _ollama_base(cfg)
    names = offline.ollama_models(base)
    model = o["model"].partition(":")[2]
    t = Table(title="Çevrimdışı mod", show_header=False)
    t.add_row("İnternet", _ok(net, "var", "yok"))
    t.add_row("Mod", {"on": "hep yerel", "off": "kapalı", "auto": "otomatik (internet yoksa yerel)"}[mode])
    t.add_row("Ollama", _ok(offline.ollama_exe() is not None, "kurulu", "kurulu değil")
              + (" · [green]çalışıyor[/]" if names is not None else " · [dim]kapalı (gerekince açılır)[/]"))
    if names is not None:
        t.add_row(f"Yerel model ({model})", _ok(offline.has_model(names, model), "hazır", "yok"))
    t.add_row("Konuşma tanıma (faster-whisper)", _ok(importlib.util.find_spec("faster_whisper") is not None,
                                                     "kurulu", "kurulu değil"))
    piper = importlib.util.find_spec("piper") is not None
    voice = piper_files(o["piper_voice"])[0].exists() if o["piper_voice"] else False
    t.add_row(f"Piper sesi ({o['piper_voice'] or '-'})", _ok(piper and voice, "hazır",
                                                             "eksik" if piper or voice else "kurulu değil"))
    local = cfg.data.get("voice", {}).get("tts_local", "")
    t.add_row("ALICE'in yerel sesi", f"[green]{local}[/]" if local else "[dim]yok (alice voice install-local)[/]")
    t.add_row("Windows sesi (son çare)", _ok(sys.platform == "win32" and o["sapi"], "açık", "kapalı"))
    console.print(t)
    if names is None or not offline.has_model(names or [], model) or not (piper and voice):
        console.print("Eksikleri kurmak için: [bold]alice offline install[/]")
    return 0


def _run(cmd: list[str], timeout: float | None = None) -> int:
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    try:
        return subprocess.run(cmd, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError) as e:
        console.print(f"[red]{escape(str(e))}[/]")
        return 1


def _download(url: str, dest: Path) -> bool:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
            if r.status_code != 200:
                console.print(f"[red]indirilemedi ({r.status_code}):[/] {url}")
                return False
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)
        return True
    except (httpx.HTTPError, OSError) as e:
        console.print(f"[red]indirilemedi:[/] {escape(str(e))}")
        return False


def _install(cfg, argv) -> int:
    import importlib.util

    from alice.voice.tts import piper_files, piper_urls

    o = offline.settings(cfg.data)
    problems: list[str] = []

    console.rule("1/4 Python paketleri (piper-tts, faster-whisper)")
    need = [p for p, mod in (("piper-tts", "piper"), ("faster-whisper", "faster_whisper"))
            if importlib.util.find_spec(mod) is None]
    if need and _run([sys.executable, "-m", "pip", "install", *need]) != 0:
        problems.append("pip install " + " ".join(need))
    elif not need:
        console.print("[green]kurulu[/]")

    console.rule(f"2/4 Piper Türkçe ses ({o['piper_voice']})")
    if o["piper_voice"]:
        model, conf = piper_files(o["piper_voice"])
        if model.exists() and conf.exists():
            console.print("[green]indirilmiş[/]")
        else:
            murl, curl = piper_urls(o["piper_voice"])
            with console.status("ses dosyası indiriliyor (~60 MB)..."):
                ok = _download(curl, conf) and _download(murl, model)
            if not ok:
                problems.append("Piper sesi indirilemedi")

    console.rule("3/4 Ollama")
    exe = offline.ollama_exe()
    if exe is None and sys.platform == "win32":
        import shutil

        if shutil.which("winget"):
            _run(["winget", "install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements",
                  "--accept-package-agreements"])
            exe = offline.ollama_exe()
    if exe is None:
        console.print("[red]Ollama bulunamadı.[/] İndir: https://ollama.com/download (kurulunca bu komutu tekrar çalıştır)")
        problems.append("Ollama kurulu değil")
    base = _ollama_base(cfg)
    ref_provider, _, model_name = o["model"].partition(":")

    console.rule(f"4/4 Yerel model ({o['base_model']} → {model_name}, bağlam {o['context']})")
    if exe is not None:
        if not offline.ensure_ollama(base, wait=20):
            problems.append("Ollama sunucusu açılamadı")
        else:
            names = offline.ollama_models(base) or []
            if not offline.has_model(names, o["base_model"]) and _run([exe, "pull", o["base_model"]]) != 0:
                problems.append(f"ollama pull {o['base_model']}")
            elif ref_provider == "ollama" and model_name != o["base_model"]:
                with tempfile.TemporaryDirectory() as d:
                    mf = Path(d) / "Modelfile"
                    mf.write_text(offline.modelfile(o["base_model"], o["context"]), encoding="utf-8")
                    if _run([exe, "create", model_name, "-f", str(mf)]) != 0:
                        problems.append(f"ollama create {model_name}")
            if o["vision_model"].startswith("ollama:"):
                vm = o["vision_model"].partition(":")[2]
                if not offline.has_model(offline.ollama_models(base) or [], vm) and _run([exe, "pull", vm]) != 0:
                    problems.append(f"ollama pull {vm}")

    if problems:
        console.print("[yellow]Eksik kalanlar:[/] " + "; ".join(problems))
        return 1
    console.print("[green]Çevrimdışı mod hazır.[/] Dene: [bold]alice offline test[/]. İnternet gidince ALICE "
                  "kendiliğinden yerele geçer.")
    return 0


def _test(cfg, argv) -> int:
    from alice.providers.router import AllProvidersFailed, Router
    from alice.voice.tts import TTSError, make_tts

    net = offline.reset(mode="on")  # internet olsa da yerel yol
    router = Router(cfg, max_wait=0, net=net)
    role = "voice" if "voice" in cfg.roles else "brain"
    console.print(f"[dim]zincir ({role}): {', '.join(router.chain(role)) or '-'}[/]")
    t0 = time.monotonic()
    try:
        with console.status("yerel model düşünüyor (ilk seferde ekran kartına yüklenir)..."):
            res = router.chat(role, [{"role": "system", "content": "Kısa ve Türkçe cevap ver."},
                                     {"role": "user", "content": "Kendini tek cümleyle tanıt."}],
                              max_tokens=400)
    except AllProvidersFailed as e:
        console.print(f"[red]yerel model cevap vermedi:[/] {escape(str(e))}\n[dim]alice offline install[/]")
        return 1
    text = (res.text or "").strip()
    console.print(f"[green]{escape(text)}[/] [dim]({res.provider}:{res.model}, {time.monotonic() - t0:.1f} sn)[/]")
    tts = make_tts(cfg)
    try:
        t1 = time.monotonic()
        audio = tts.synth(text or "Merhaba, ben ALICE. İnternet olmadan da buradayım.", "calm")
    except TTSError as e:
        console.print(f"[red]yerel ses yok:[/] {escape(str(e))}")
        return 1
    console.print(f"[dim]ses: {tts.last}, {time.monotonic() - t1:.1f} sn[/]")
    from alice.voice.audio import Player

    player = Player()
    if player.available:
        player.play(audio)
    return 0
