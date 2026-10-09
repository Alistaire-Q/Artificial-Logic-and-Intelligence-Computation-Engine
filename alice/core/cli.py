"""Çekirdek komutları.

  alice core run [--tray] [--once]   çekirdeği çalıştır (--tray: sistem tepsisinde)
  alice core install | uninstall     oturum açılınca otomatik başlat / kaldır
  alice core status                  çalışıyor mu, hangi mod, son olaylar
  alice core mode <mod|auto>         modu elle sabitle (normal, dev, flight, quiet) ya da otomatiğe dön
  alice core stats <anahtar> [süre]  ör. alice core stats pc.gpu.temp 1d
  alice rules [list|add "cümle"|rm|on|off <id>]
  alice tasks [list|add "cümle"|rm|run <id>]
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape

from alice.config import load_config

console = Console(highlight=False)


def _confirm(question: str) -> bool:
    return console.input(f"{question} [bold][e/h][/] > ").strip().lower() in ("e", "evet", "y", "yes", "")


def core_cmd(argv: list[str]) -> int:
    from alice.core import service

    sub = argv[0] if argv else "status"
    if sub == "run":
        cfg = load_config()
        if not service.acquire_lock():
            console.print("[yellow]çekirdek zaten çalışıyor[/]")
            return 1
        from alice.decider import Decider
        from alice.providers.router import Router

        router = Router(cfg)
        core = service.Core(cfg, decider=Decider.from_config(cfg, router), run_agent=service.make_agent_runner(cfg))
        voice = _start_voice(cfg, core) if cfg.data.get("voice", {}).get("enabled") and "--once" not in argv else None
        sims = _start_sims(cfg, core) if "--once" not in argv else None
        phone = _start_phone(cfg, core) if cfg.data.get("phone", {}).get("enabled") and "--once" not in argv else None
        quick = _start_quick(cfg) if cfg.data.get("quick", {}).get("enabled", True) and "--once" not in argv else None
        try:
            if "--once" in argv:
                core.tick()
                for e in reversed(core.store.events(limit=10)):
                    console.print(escape(e.line()))
                return 0
            console.print(f"[bold magenta]ALICE çekirdek[/] çalışıyor, her {core.interval:g} sn. "
                          "[dim]Durdurmak için Ctrl+C[/]")
            if "--tray" in argv and service.run_tray(core):
                return 0
            core.run_forever()
        except KeyboardInterrupt:
            core.stop()
        finally:
            if voice is not None:
                voice.stop()
            if quick is not None:
                quick.stop()
            if sims is not None:
                sims.stop()
            if phone is not None:
                phone.stop()
            service.release_lock()
        return 0
    if sub == "install":
        p = service.install_autostart()
        console.print(f"[green]otomatik başlatma kuruldu:[/] {p}")
        if "pystray" not in sys.modules:
            import importlib.util

            if importlib.util.find_spec("pystray") is None:
                console.print("[dim]Tepsi simgesi için: pip install 'alice-harness[core]'[/]")
        return 0
    if sub == "uninstall":
        console.print("[green]kaldırıldı[/]" if service.uninstall_autostart() else "[dim]zaten kurulu değil[/]")
        return 0
    if sub == "mode":
        if len(argv) < 2 or argv[1] not in ("auto", "normal", "dev", "flight", "quiet"):
            console.print("kullanım: alice core mode <auto|normal|dev|flight|quiet>")
            return 2
        service.force_mode(None if argv[1] == "auto" else argv[1])
        console.print(f"mod: [bold]{argv[1]}[/]")
        return 0
    if sub == "stats":
        from alice.core.store import Store, parse_time

        if len(argv) < 2:
            console.print("kullanım: alice core stats <anahtar|glob> [süre, ör. 1d]")
            return 2
        st = Store()
        since = parse_time(argv[2] if len(argv) > 2 else "1d")
        keys = st.keys(argv[1], since=since) if any(c in argv[1] for c in "*?[") else [argv[1]]
        for k in keys:
            console.print(escape(st.stats(k, since).line()))
        return 0
    if sub == "status":
        s = service.read_state()
        if s.get("running"):
            p = s.get("policy", {})
            console.print(f"[green]çalışıyor[/] (pid {s.get('pid')}), mod: [bold]{s.get('mode')}[/]"
                          f"{' (elle sabit)' if s.get('forced') else ''}, en düşük bildirim: {p.get('min_severity')}, "
                          f"yerel GPU: {p.get('local_gpu')}")
        else:
            console.print("[yellow]çalışmıyor[/]  [dim]başlat: alice core run · kalıcı: alice core install[/]")
        from alice.core.store import Store

        evs = Store().events(limit=10)
        if evs:
            console.print("[bold]Son olaylar[/]")
            for e in reversed(evs):
                console.print("  " + escape(e.line()))
        return 0
    console.print(__doc__)
    return 2


def _start_phone(cfg, core):
    from alice.app.phone import PhoneServer

    try:
        ps = PhoneServer(cfg, core.store)
    except OSError as e:
        console.print(f"[yellow]telefon köprüsü açılamadı:[/] {e}")
        return None
    console.print(f"telefon: {ps.url}")
    return ps


def _start_sims(cfg, core):
    """DCS ve War Thunder dinleyicilerini başlat."""
    from alice.sims.hub import SimHub

    return SimHub(cfg.data.get("sims", {}), core.store, core.notifier, processes=lambda: core.pc.processes).start()


def _start_quick(cfg):
    """Hızlı bar kısayollarını çekirdekle birlikte başlat (pynput yoksa sessizce atla)."""
    import importlib.util

    if importlib.util.find_spec("pynput") is None:
        return None
    from alice.app.quick import QuickBar

    qb = QuickBar(cfg)
    if not qb.start_hotkeys():
        qb.stop()
        return None
    return qb


def _start_voice(cfg, core):
    """Sesli asistanı çekirdekle aynı süreçte başlat; bildirimler de seslendirilsin."""
    import threading

    from alice.voice.assistant import VoiceAssistant, build_voice_agent, make_live
    from alice.voice.audio import mic_available
    from alice.voice.stt import make_stt

    v = cfg.data.get("voice", {})
    agent, speaker, _ = build_voice_agent(cfg)

    def speak(n, pol):
        # Sessiz modda (voice = false) bildirim sadece yazılı gelir.
        if pol.voice:
            speaker.say(n.body or n.title, n.emotion or None, severity=n.severity, topic=n.topic)

    core.notifier.channels.append(speak)
    if not mic_available():
        console.print("[yellow]mikrofon yok;[/] ALICE sadece bildirimleri seslendirecek")
        return None
    va = VoiceAssistant(agent, speaker, make_stt(cfg), ptt_key=v.get("ptt_key", ""),
                        wake_model=v.get("wake_model", ""), wake_phrase=v.get("wake_phrase", True),
                        barge_in=v.get("barge_in", True), follow_up=float(v.get("follow_up", 8)),
                        live=make_live(cfg, agent))
    threading.Thread(target=va.run, daemon=True, name="alice-voice").start()
    return va


def rules_cmd(argv: list[str]) -> int:
    from alice.core.rules import RuleSet, parse_rule

    rs = RuleSet()
    sub = argv[0] if argv else "list"
    if sub == "list":
        for r in rs.rules:
            console.print(escape(r.summary()) + (f"  [dim]{escape(r.description)}[/]" if r.description else ""))
        if not rs.rules:
            console.print("[dim]kural yok[/]")
        return 0
    if sub == "add" and len(argv) > 1:
        from alice.core.store import Store
        from alice.providers.router import AllProvidersFailed, Router

        text = " ".join(argv[1:])
        try:
            with console.status("kurala çevriliyor..."):
                rule = parse_rule(text, Router(load_config()), Store())
        except (ValueError, TypeError, AllProvidersFailed) as e:
            console.print(f"[red]anlaşılamadı:[/] {escape(str(e))}")
            return 1
        console.print(f"[bold]{escape(rule.summary())}[/]\n  mesaj: {escape(rule.message)}")
        if rule.confirm:
            console.print(f"  Jev'e sorulacak: {escape(rule.confirm)}")
        if rule.action == "agent":
            console.print(f"  tetiklenince ALICE: {escape(rule.prompt)}")
        if not _confirm("Kurulsun mu?"):
            return 1
        rs.add(rule)
        console.print("[green]kuruldu[/]")
        return 0
    if sub in ("rm", "on", "off") and len(argv) > 1:
        ok = rs.remove(argv[1]) if sub == "rm" else rs.set_enabled(argv[1], sub == "on")
        console.print("[green]tamam[/]" if ok else f"[red]kural yok: {escape(argv[1])}[/]")
        return 0 if ok else 1
    console.print(__doc__)
    return 2


def tasks_cmd(argv: list[str]) -> int:
    from alice.core.schedule import Cron, TaskStore, parse_task

    ts = TaskStore()
    sub = argv[0] if argv else "list"
    if sub == "list":
        for t in ts.tasks:
            console.print(escape(t.summary()))
            if t.last_result:
                console.print(f"  [dim]son sonuç: {escape(t.last_result[:150])}[/]")
        if not ts.tasks:
            console.print("[dim]zamanlanmış görev yok[/]")
        return 0
    if sub == "add" and len(argv) > 1:
        from alice.providers.router import AllProvidersFailed, Router

        text = " ".join(argv[1:])
        try:
            with console.status("zamanlamaya çevriliyor..."):
                task = parse_task(text, Router(load_config()))
        except (ValueError, TypeError, AllProvidersFailed) as e:
            console.print(f"[red]anlaşılamadı:[/] {escape(str(e))}")
            return 1
        console.print(f"[bold]Ne zaman:[/] {Cron(task.cron).describe()}  [dim]({task.cron})[/]\n"
                      f"[bold]Ne yapılacak:[/] {escape(task.prompt)}\n"
                      f"[bold]Kaçırılırsa:[/] {'açılışta telafi edilir' if task.catch_up else 'atlanır'}")
        if not _confirm("Kurulsun mu?"):
            return 1
        ts.add(task)
        console.print(f"[green]kuruldu[/] ({task.id}), sonraki: {task.next_run():%d.%m %H:%M}")
        from alice.core.service import read_state

        if not read_state().get("running"):
            console.print("[yellow]çekirdek çalışmıyor;[/] görevin çalışması için [bold]alice core install[/]")
        return 0
    if sub in ("rm", "run") and len(argv) > 1:
        t = ts.get(argv[1])
        if t is None:
            console.print(f"[red]görev yok: {escape(argv[1])}[/]")
            return 1
        if sub == "rm":
            ts.remove(t.id)
        else:
            t.pending = True
            ts.save()
        console.print("[green]tamam[/]" + (" (bir sonraki çekirdek turunda çalışacak)" if sub == "run" else ""))
        return 0
    console.print(__doc__)
    return 2
