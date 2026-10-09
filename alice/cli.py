"""ALICE komut satırı.

  alice                      etkileşimli oturum (REPL)
  alice -p "görev"           tek seferlik çalıştır, sonucu yaz, çık
  alice init                 ~/.alice altında config ve .env oluştur
  alice doctor               anahtarları, modelleri ve opsiyonel bağımlılıkları test et
  alice models [--write]     canlı ücretsiz model kataloğunu tara, rol zinciri öner
  alice reflex "hedef"       Jev/System One reflex döngüsünü tek başına çalıştır
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from alice import __version__
from alice.agent import Agent
from alice.config import Config, alice_home, init_home, load_config
from alice.decider import Decider
from alice.permissions import MODES, Permissions
from alice.providers.router import AllProvidersFailed, Router, parse_ref
from alice.tools import ToolContext, all_tools, cleanup
from alice.tools.base import ToolResult

console = Console(highlight=False)

HELP = """[bold]Komutlar[/]
  /help                 bu yardım
  /mode [ask|auto|yolo] izin modunu göster/değiştir
  /model                rol zincirlerini ve durumlarını göster
  /model <rol> <ref>    rolü tek modele sabitle, ör. /model brain groq:openai/gpt-oss-120b
  /model <rol> reset    config'teki zincire geri dön
  /usage                bugünkü istek/token kullanımı
  /tools                araç listesi
  /compact              context'i şimdi özetle
  /clear                konuşmayı sıfırla
  /sessions             kayıtlı oturumlar
  /resume [id]          oturumu geri yükle (id yoksa en sonuncusu)
  /cwd [yol]            çalışma klasörünü göster/değiştir
  /exit                 çık
Çalışırken [bold]Ctrl+C[/] mevcut işi durdurur. Fareyi ekranın sol üst köşesine götürmek fare/klavye kontrolünü keser."""


class RichUI:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def assistant_text(self, text: str, source: str) -> None:
        console.print(Markdown(text))
        if self.verbose:
            console.print(f"[dim]  ({source})[/]")

    def tool_start(self, name: str, summary: str) -> None:
        console.print(f"[cyan]●[/] [bold]{name}[/] [dim]{summary}[/]")

    def tool_end(self, name: str, result: ToolResult) -> None:
        lines = result.text.strip().splitlines()
        shown = "\n".join(f"  ⎿ {line[:200]}" if i == 0 else f"    {line[:200]}" for i, line in enumerate(lines[:6]))
        if len(lines) > 6:
            shown += f"\n    [dim]... +{len(lines) - 6} satır[/]"
        if result.images:
            shown += f"\n    [dim](+{len(result.images)} görüntü)[/]"
        console.print(shown, style="red" if result.is_error else "dim", markup=True)

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        color = "red" if risk == "danger" else "yellow"
        console.print(f"[{color}]? İzin gerekiyor[/] [bold]{name}[/] ({risk})\n  {summary}")
        while True:
            ans = console.input("  [bold][y][/]evet  [bold][n][/]hayır  [bold][a][/]bu araca hep izin ver > ").strip().lower()
            if ans in ("y", "e", "evet", "yes", ""):
                return "y"
            if ans in ("n", "h", "hayır", "no"):
                return "n"
            if ans in ("a", "always", "hep"):
                return "a"

    def info(self, msg: str) -> None:
        console.print(f"[yellow]{msg}[/]")

    @contextmanager
    def thinking(self, label: str):
        with console.status(f"[dim]{label}...[/]", spinner="dots"):
            yield


def build(cfg: Config, cwd: Path, mode: str, verbose: bool = False) -> tuple[Agent, Router, ToolContext]:
    ui = RichUI(verbose)
    router = Router(cfg, on_event=(lambda s: console.print(f"[dim]  ↪ {s}[/]")) if verbose else None)
    decider = Decider.from_config(cfg, router, on_event=lambda s: console.print(f"[dim]  ↪ {s}[/]"))
    ctx = ToolContext(cwd=cwd, config=cfg, router=router, decider=decider,
                      emit=lambda s: console.print(f"[dim]  {s}[/]"))
    agent = Agent(cfg, router, all_tools(), ctx, Permissions(mode), ui, decider)
    return agent, router, ctx


def _check_ready(router: Router) -> bool:
    if router.first_available("brain"):
        return True
    console.print("[red]Kullanılabilir beyin modeli yok.[/] Önce [bold]alice init[/] çalıştır ve "
                  f"{alice_home() / '.env'} dosyasına en az bir API anahtarı ekle (en kolayı GEMINI_API_KEY).")
    return False


# ----------------------------------------------------------------------- REPL
def repl(agent: Agent, router: Router, ctx: ToolContext, cfg: Config, resume: str | None) -> None:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    from alice import session as sess

    sid = sess.new_session_id()
    if resume is not None:
        loaded = sess.load(resume or None)
        if loaded:
            sid, msgs = loaded
            agent.messages = agent.messages[:1] + msgs
            console.print(f"[green]oturum yüklendi:[/] {sid} ({len(msgs)} mesaj)")
        else:
            console.print("[yellow]yüklenecek oturum bulunamadı[/]")

    alice_home().mkdir(parents=True, exist_ok=True)
    ps: PromptSession = PromptSession(history=FileHistory(str(alice_home() / "history")))
    brain = router.first_available("brain") or "yok"
    console.print(f"[bold magenta]ALICE[/] v{__version__}  [dim]beyin: {brain} | karar: {', '.join(agent.decider.names) if agent.decider else '-'} "
                  f"| izin: {agent.permissions.mode} | {ctx.cwd}[/]\n[dim]/help ile komutlar[/]")
    while True:
        try:
            text = ps.prompt("\n› ").strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue
        if text.startswith("/"):
            if handle_slash(text, agent, router, ctx, cfg) == "exit":
                break
            continue
        t0 = time.monotonic()
        agent.run_turn(text)
        console.print(f"[dim]  {time.monotonic() - t0:.1f} sn[/]")
        try:
            sess.save(sid, agent.messages, ctx.cwd)
        except OSError:
            pass
    cleanup(ctx)


def handle_slash(text: str, agent: Agent, router: Router, ctx: ToolContext, cfg: Config) -> str | None:
    from alice import session as sess

    parts = text.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("/exit", "/quit", "/q"):
        return "exit"
    if cmd == "/help":
        console.print(HELP)
    elif cmd == "/clear":
        agent.reset()
        console.print("[green]konuşma sıfırlandı[/]")
    elif cmd == "/compact":
        console.print("[green]özetlendi[/]" if agent.compact() else "[yellow]özetlenecek bir şey yok[/]")
    elif cmd == "/mode":
        if args and args[0] in MODES:
            agent.permissions.mode = args[0]
        console.print(f"izin modu: [bold]{agent.permissions.mode}[/]")
    elif cmd == "/model":
        if len(args) >= 2:
            role, ref = args[0], args[1]
            if ref == "reset":
                router.role_overrides.pop(role, None)
            else:
                try:
                    parse_ref(ref)
                except ValueError as e:
                    console.print(f"[red]{e}[/]")
                    return None
                router.role_overrides[role] = [ref]
        print_status(router)
    elif cmd == "/usage":
        print_usage(router)
    elif cmd == "/tools":
        for t in agent.tools.values():
            console.print(f"[bold]{t.name}[/] [dim]({t.risk})[/] {t.description.splitlines()[0][:110]}")
    elif cmd == "/sessions":
        for sid, cwd, first in sess.list_sessions():
            console.print(f"[bold]{sid}[/] [dim]{cwd}[/]  {first}")
    elif cmd == "/resume":
        loaded = sess.load(args[0] if args else None)
        if loaded:
            agent.messages = agent.messages[:1] + loaded[1]
            console.print(f"[green]yüklendi:[/] {loaded[0]}")
        else:
            console.print("[yellow]oturum yok[/]")
    elif cmd == "/cwd":
        if args:
            p = ctx.resolve(" ".join(args))
            if p.is_dir():
                ctx.cwd = p
            else:
                console.print(f"[red]klasör yok: {p}[/]")
        console.print(str(ctx.cwd))
    else:
        console.print(f"[red]bilinmeyen komut {cmd}[/] (/help)")
    return None


def print_status(router: Router) -> None:
    t = Table(title="Rol zincirleri", show_lines=False)
    t.add_column("rol")
    t.add_column("model")
    t.add_column("durum")
    for role, rows in router.status().items():
        for i, (ref, st) in enumerate(rows):
            style = "green" if st == "hazır" else "dim"
            t.add_row(role if i == 0 else "", ref, f"[{style}]{st}[/]")
    console.print(t)


def print_usage(router: Router) -> None:
    t = Table(title="Bugünkü kullanım")
    for c in ("model", "istek", "hata", "girdi tok", "çıktı tok"):
        t.add_column(c)
    for ref, row in sorted(router.usage.today().items()):
        t.add_row(ref, str(row["requests"]), str(row["errors"]), str(row["prompt_tokens"]), str(row["completion_tokens"]))
    console.print(t)


# ------------------------------------------------------------------- doctor
def doctor(cfg: Config) -> int:
    router = Router(cfg, max_wait=0)
    console.print(f"[bold]ALICE home:[/] {alice_home()}  config: {cfg.path or '(varsayılan)'}")
    t = Table(title="Sağlayıcılar")
    for c in ("sağlayıcı", "anahtar", "test modeli", "sonuç"):
        t.add_column(c)
    refs_by_provider: dict[str, str] = {}
    for chain in cfg.roles.values():
        for ref in chain:
            p = ref.split(":", 1)[0]
            refs_by_provider.setdefault(p, ref)
    for name, pc in router.providers_cfg.items():
        key = "gerekmez" if not pc.needs_key else ("var" if pc.api_key else f"[dim]{pc.api_key_env} yok[/]")
        ref = refs_by_provider.get(name, "")
        if not pc.enabled or not ref:
            t.add_row(name, key, ref, "[dim]atlandı[/]")
            continue
        t0 = time.monotonic()
        try:
            res = router.chat(ref, [{"role": "user", "content": "Reply with just: ok"}], max_tokens=50)
            t.add_row(name, key, ref, f"[green]çalışıyor[/] {time.monotonic() - t0:.1f} sn: {res.text.strip()[:20]!r}")
        except AllProvidersFailed as e:
            t.add_row(name, key, ref, f"[red]{e.errors[-1][:110] if e.errors else 'hata'}[/]")
    console.print(t)

    decider = Decider.from_config(cfg, router)
    console.print(f"[bold]Karar motorları:[/] {', '.join(decider.names)}")
    from alice.decider import Choice, DeciderError, Noul

    try:
        d = decider.decide("A login form is visible with username, password fields and a 'Sign in' button. Goal: sign in.",
                           {"next": Choice("Next action?", {"type_user": "Type username", "click_signin": "Click Sign in",
                                                            "scroll": "Scroll down"}),
                            "done": Noul("Is the user already signed in?")})
        console.print(f"  test kararı: next={d['next'].value} ({d['next'].confidence:.2f}), done={d['done'].value:.2f} "
                      f"[dim]{d.latency * 1000:.0f} ms, {d.engine}[/]")
    except DeciderError as e:
        console.print(f"  [red]karar motoru çalışmıyor: {e}[/]")

    console.print("[bold]Opsiyonel bileşenler:[/]")
    for mod, why in (("mss", "ekran görüntüsü"), ("pyautogui", "fare/klavye"), ("pyperclip", "pano, Türkçe yazma"),
                     ("pygetwindow", "pencere yönetimi (Windows/macOS)"), ("pywinauto", "UI Automation (Windows)"),
                     ("playwright", "tarayıcı")):
        ok = importlib.util.find_spec(mod) is not None
        console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {mod:<12} {why}")
    return 0


# ------------------------------------------------------------------- models
def models_cmd(cfg: Config, write: bool) -> int:
    from alice.providers.discovery import discover, suggest_chains

    router = Router(cfg)
    with console.status("modeller taranıyor..."):
        found, errors = discover(router)
    t = Table(title=f"Bulunan modeller ({len(found)})")
    for c in ("ref", "context", "tools", "vision"):
        t.add_column(c)
    for f in found[:80]:
        yn = lambda v: "?" if v is None else ("✓" if v else "✗")  # noqa: E731
        t.add_row(f.ref, str(f.context or "?"), yn(f.tools), yn(f.vision))
    console.print(t)
    for p, e in errors.items():
        console.print(f"[dim]{p}: {e}[/]")
    chains = suggest_chains(found)
    console.print("[bold]Önerilen zincirler:[/]")
    for role, refs in chains.items():
        console.print(f"  {role}: {refs}")
    if write:
        path = write_roles(chains)
        console.print(f"[green]yazıldı:[/] {path}")
    else:
        console.print("[dim]config'e yazmak için: alice models --write[/]")
    return 0


def write_roles(chains: dict[str, list[str]]) -> Path:
    """config.toml'daki ilgili rol satırlarını değiştir (boş öneri olan rollere dokunma)."""
    init_home()
    path = alice_home() / "config.toml"
    text = path.read_text(encoding="utf-8")
    for role, refs in chains.items():
        if not refs:
            continue
        block = f"{role} = [\n" + "".join(f'    "{r}",\n' for r in refs) + "]"
        rx = re.compile(rf"(?ms)^{role}\s*=\s*\[.*?^\]")
        if rx.search(text):
            text = rx.sub(lambda _m, b=block: b, text, count=1)
        else:
            text = text.replace("[roles]\n", f"[roles]\n{block}\n", 1)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------- reflex
def reflex_cmd(cfg: Config, a: argparse.Namespace) -> int:
    from alice.reflex.loop import ReflexLoop, make_text_writer
    from alice.reflex.observers import BrowserObserver, DesktopObserver

    router = Router(cfg)
    decider = Decider.from_config(cfg, router, on_event=lambda s: console.print(f"[dim]{s}[/]"))
    ctx = ToolContext(cwd=Path.cwd(), config=cfg, router=router, decider=decider)
    inputs = dict(kv.split("=", 1) for kv in a.input or [])
    try:
        observer = BrowserObserver(ctx, a.url) if a.target == "browser" else DesktopObserver(ctx, a.title)
        loop = ReflexLoop(observer, decider, make_text_writer(router),
                          min_confidence=float(cfg.decider.get("min_confidence", 0.55)),
                          emit=lambda s: console.print(f"[cyan]{s}[/]"))
        res = loop.run(a.goal, max_steps=a.max_steps, inputs=inputs)
        console.print(res.report())
        if a.target == "browser" and not a.close:
            console.input("[dim]tarayıcıyı kapatmak için Enter[/]")
    finally:
        cleanup(ctx)
    return 0 if res.status == "done" else 1


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    if argv and argv[0] == "init":
        for p in init_home(force="--force" in argv):
            console.print(f"[green]oluşturuldu:[/] {p}")
        console.print(f"Şimdi {alice_home() / '.env'} dosyasına API anahtarlarını yaz, sonra [bold]alice doctor[/].")
        return 0
    if argv and argv[0] == "doctor":
        return doctor(load_config())
    if argv and argv[0] == "models":
        return models_cmd(load_config(), "--write" in argv)
    if argv and argv[0] == "reflex":
        rp = argparse.ArgumentParser(prog="alice reflex")
        rp.add_argument("goal")
        rp.add_argument("--target", choices=["browser", "desktop"], default="browser")
        rp.add_argument("--url")
        rp.add_argument("--title", help="desktop: pencere başlığı parçası")
        rp.add_argument("--input", action="append", help="alan=metin (tekrarlanabilir)")
        rp.add_argument("--max-steps", type=int, default=25)
        rp.add_argument("--close", action="store_true", help="bitince tarayıcıyı hemen kapat")
        return reflex_cmd(load_config(), rp.parse_args(argv[1:]))

    ap = argparse.ArgumentParser(prog="alice", description="ALICE ajan harness")
    ap.add_argument("prompt", nargs="*", help="ilk mesaj (verilirse REPL bu mesajla başlar)")
    ap.add_argument("-p", "--print", dest="oneshot", action="store_true", help="tek seferlik çalıştır ve çık")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--yolo", action="store_true", help="hiçbir izin sorma (tam erişim)")
    ap.add_argument("--cwd", type=Path)
    ap.add_argument("--model", help="beyin rolünü tek modele sabitle, ör. gemini:gemini-flash-latest")
    ap.add_argument("--resume", nargs="?", const="", help="son (ya da verilen id'li) oturumu devam ettir")
    ap.add_argument("-v", "--verbose", action="store_true", help="hangi modelin cevap verdiğini göster")
    ap.add_argument("--version", action="version", version=f"alice {__version__}")
    a = ap.parse_args(argv)

    cwd = (a.cwd or Path.cwd()).resolve()
    cfg = load_config(cwd=cwd)
    mode = "yolo" if a.yolo else (a.mode or cfg.agent.get("permission_mode", "auto"))
    agent, router, ctx = build(cfg, cwd, mode, a.verbose)
    if a.model:
        router.role_overrides["brain"] = [a.model]
    if not _check_ready(router):
        return 2
    prompt = " ".join(a.prompt).strip()
    if a.oneshot:
        if not prompt:
            prompt = sys.stdin.read().strip()
        try:
            out = agent.run_turn(prompt)
        finally:
            cleanup(ctx)
        return 0 if out else 1
    if prompt:
        agent.run_turn(prompt)
    repl(agent, router, ctx, cfg, a.resume)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
