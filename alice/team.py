"""Takım modu: ALICE orkestratör olur, büyük bir işi paralel çalışan ajanlara böler.

Her üye kendi thread'inde, kendi bağlamı ve çalışma klasörüyle çalışan bir alt ajandır. Orkestratör her üyeye
görev, sınırlar, yazma kapsamı, model ve düşünme seviyesi verir. Üyeler birbirine ve ALICE'e mesaj atabilir
(team_send), cevap bekleyebilir (team_wait); gelen mesajlar her araç adımından sonra konuşmalarına eklenir.

Her takım bir "proje"dir: ~/.alice/projects/<id>.json'a kaydedilir, uygulamanın Projeler sayfasında üyeler ayrı
konuşmalar gibi görünür; uygulama kapanıp açılsa da proje yüklenir ve biten üyeye yazınca kaldığı yerden devam eder.

Güvenlik: üyeler sadece kod işi araçlarını kullanır (dosya, kabuk, web, git); ekran/tarayıcı araçları tek
kullanıcı ekranını paylaştığı için kapalı. Yazma kapsamı verilen üye kapsam dışına dosya araçlarıyla yazamaz;
"readonly" üye hiç yazamaz. İzin soruları üyenin adıyla kullanıcıya gider.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, obj

ORCHESTRATOR = "alice"
USER = "kullanıcı"  # Projeler sayfasından yazan kullanıcı
# Üyelerin kullanabildiği araçlar: kod işi. Ekran, tarayıcı, masaüstü araçları kullanıcının tek ekranını paylaşır.
MEMBER_TOOLS = {"read_file", "write_file", "edit_file", "multi_edit", "apply_patch", "file_ops", "list_dir", "glob",
                "grep", "run_command", "process_output", "process_stop", "web_fetch", "web_search", "http_request",
                "todo_write", "git"}
WRITE_TOOLS = {"write_file", "edit_file", "multi_edit", "apply_patch", "file_ops"}
ISOLATIONS = ("shared", "worktree", "readonly")
THINKING = ("off", "low", "medium", "high", "default")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
DONE_STATES = ("done", "failed", "stopped")
SAVE_EVERY = 1.5  # saniye: canlı güncellemelerde diske en sık bu aralıkla yazılır


@dataclass
class Message:
    sender: str
    to: str
    text: str
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        return f"[{self.sender} -> {self.to}] {self.text}"


@dataclass
class Member:
    name: str
    task: str
    constraints: str = ""
    scope: list[str] = field(default_factory=list)  # yazılabilecek yollar (boş: tüm çalışma klasörü)
    model: str = "brain"  # rol adı (brain, fast...) ya da sağlayıcı:model
    thinking: str = "default"
    isolation: str = "shared"
    max_steps: int = 60
    cwd: Path = Path(".")
    branch: str = ""
    status: str = "queued"  # queued | running | waiting | done | failed | stopped
    result: str = ""
    steps: int = 0
    last: str = ""
    started: float = 0.0
    ended: float = 0.0
    agent: Any = None
    thread: threading.Thread | None = None
    files: list[str] = field(default_factory=list)  # dosya araçlarıyla değiştirdiği yollar (çalışma klasörüne göre)
    saved_messages: list[dict[str, Any]] = field(default_factory=list)  # diskten yüklenmiş konuşma (sistem istemi hariç)
    resume_wanted: bool = False  # bitmişken mesaj geldi ama paralel sınırı dolu: yer açılınca devam edecek
    reported: bool = False  # bitişi orkestratöre bildirildi mi

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"name": self.name, "task": self.task, "model": self.model, "thinking": self.thinking,
                "isolation": self.isolation, "status": self.status, "steps": self.steps, "last": self.last,
                "branch": self.branch, "scope": self.scope, "result": self.result[:600], "files": self.files[-50:],
                "constraints": self.constraints, "secs": int(now - self.started) if self.started else 0}

    def messages(self) -> list[dict[str, Any]]:
        """Sistem istemi hariç konuşma: canlı ajandan ya da diskten."""
        if self.agent is not None:
            return self.agent.messages[1:]
        return self.saved_messages


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path)


def in_scope(path: Path, scope: list[Path]) -> bool:
    return not scope or any(path == s or s in path.parents for s in scope)


def patch_paths(patch: str) -> list[str]:
    from alice.tools.patch import ADD, DELETE, MOVE, UPDATE

    out = []
    for line in (patch or "").splitlines():
        for prefix in (ADD, DELETE, UPDATE, MOVE):
            if line.startswith(prefix):
                out.append(line[len(prefix):].strip())
    return out


def write_targets(tool: str, args: dict[str, Any]) -> list[str]:
    """Dosya aracının yazacağı yollar (kapsam kontrolü için)."""
    if tool == "apply_patch":
        return patch_paths(str(args.get("patch") or ""))
    out = [str(args[k]) for k in ("path", "destination") if args.get(k)]
    out += [str(p) for p in args.get("paths") or []]
    return out


def projects_dir() -> Path:
    from alice.config import alice_home

    d = alice_home() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slim(messages: list[dict[str, Any]], limit: int = 6000) -> list[dict[str, Any]]:
    """Diske yazılacak konuşma: görseller atılır, çok uzun araç çıktıları kısaltılır."""
    from alice.providers.router import strip_images

    out = []
    for m in strip_images(messages):
        m = dict(m)
        if isinstance(m.get("content"), str) and len(m["content"]) > limit:
            m["content"] = m["content"][:limit] + f"\n... [{len(m['content']) - limit} karakter kaydedilmedi]"
        out.append(m)
    return out


def list_projects() -> list[dict[str, Any]]:
    out = []
    for f in projects_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        members = data.get("members") or []
        out.append({"id": data.get("id") or f.stem, "title": data.get("title") or "", "goal": data.get("goal", ""),
                    "created": data.get("created", 0), "updated": data.get("updated", 0),
                    "origin": data.get("origin", "alice"), "cwd": data.get("cwd", ""),
                    "members": [{"name": m.get("name"), "status": m.get("status")} for m in members]})
    return sorted(out, key=lambda x: -float(x.get("updated") or x.get("created") or 0))


class Team:
    def __init__(self, parent_ctx: ToolContext, goal: str, max_parallel: int = 5,
                 publish: Callable[[dict[str, Any]], None] | None = None, origin: str = "alice",
                 project_id: str = "", title: str = ""):
        self.parent_ctx = parent_ctx
        self.goal = goal
        self.id = project_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.title = title or (goal.strip().splitlines()[0][:70] if goal.strip() else "Proje")
        self.origin = origin  # alice: ALICE kurdu | user: kullanıcı Projeler sayfasından kurdu
        self.cwd = parent_ctx.cwd
        self.created = time.time()
        self._saved_at = 0.0
        self._save_lock = threading.Lock()
        self.max_parallel = max(1, max_parallel)
        self.members: dict[str, Member] = {}
        self.log: list[Message] = []
        self.inbox: dict[str, list[Message]] = {ORCHESTRATOR: []}
        self.cond = threading.Condition()
        self.perm_lock = threading.Lock()  # izin pencereleri tek tek açılsın
        self.publish = publish or (lambda ev: None)
        self.started = time.time()
        self.stopped = False
        self.roles: list[str] = []

    # ------------------------------------------------------------- mesajlar
    def send(self, sender: str, to: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ToolError("mesaj boş")
        targets = [n for n in self.members if n != sender] + ([ORCHESTRATOR] if sender != ORCHESTRATOR else []) \
            if to == "all" else [to]
        unknown = [t for t in targets if t != ORCHESTRATOR and t not in self.members]
        if unknown:
            raise ToolError(f"takımda böyle biri yok: {', '.join(unknown)}. Üyeler: {', '.join(self.members)}, "
                            f"orkestratör: {ORCHESTRATOR}")
        resume = []
        with self.cond:
            msg = Message(sender, to, text)
            self.log.append(msg)
            for t in targets:
                self.inbox.setdefault(t, []).append(msg)
                m = self.members.get(t)
                if m is not None and m.status in DONE_STATES and not self.stopped and \
                        (m.status != "stopped" or sender == USER):  # durdurulanı sadece kullanıcı devam ettirir
                    resume.append(m)
            self.cond.notify_all()
        for m in resume:  # bitmiş üyeye iş geldi: konuşmasına kaldığı yerden devam etsin
            self._launch(m, resume=True)
        self._changed()
        return f"gönderildi: {', '.join(targets)}"

    def take(self, name: str) -> list[Message]:
        with self.cond:
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_messages(self, name: str, timeout: float, cancelled: Callable[[], bool] = lambda: False) -> list[Message]:
        end = time.monotonic() + timeout
        with self.cond:
            while not self.inbox.get(name) and not self.stopped and not cancelled():
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            msgs, self.inbox[name] = self.inbox.get(name, []), []
            return msgs

    def wait_orchestrator(self, timeout: float, cancelled: Callable[[], bool]) -> tuple[list[Message], list[Member]]:
        """ALICE'e mesaj gelene, bir üye bitene ya da süre dolana kadar bekle."""
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                fresh = any(m.status in DONE_STATES and not m.reported for m in self.members.values())
                if self.inbox.get(ORCHESTRATOR) or fresh or self.all_done() or cancelled():
                    break
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            msgs, self.inbox[ORCHESTRATOR] = self.inbox.get(ORCHESTRATOR, []), []
            finished = [m for m in self.members.values() if m.status in DONE_STATES and not m.reported]
            for m in finished:
                m.reported = True
            return msgs, finished

    def all_done(self) -> bool:
        return all(m.status in DONE_STATES and not m.resume_wanted for m in self.members.values())

    # --------------------------------------------------------------- üyeler
    def add(self, spec: dict[str, Any]) -> Member:
        name = str(spec.get("name") or "").strip().lower()
        if not NAME_RX.match(name) or name in (ORCHESTRATOR, "all"):
            raise ToolError(f"geçersiz üye adı '{name}': küçük harf/rakam/-/_, en çok 24 karakter; 'alice' ve 'all' "
                            "ayrılmış")
        if name in self.members:
            raise ToolError(f"'{name}' adı iki kez kullanılmış")
        task = str(spec.get("task") or "").strip()
        if not task:
            raise ToolError(f"{name}: task boş")
        iso = str(spec.get("isolation") or "shared")
        if iso not in ISOLATIONS:
            raise ToolError(f"{name}: isolation {', '.join(ISOLATIONS)} olmalı")
        think = str(spec.get("thinking") or "default")
        if think not in THINKING:
            raise ToolError(f"{name}: thinking {', '.join(THINKING)} olmalı")
        model = str(spec.get("model") or "brain").strip()
        router = self.parent_ctx.router
        try:
            ok = router is None or bool(router.chain(model))
        except KeyError:
            ok = False
        if not ok:
            raise ToolError(f"{name}: '{model}' için model yok. Rol adı (brain, fast...) ya da sağlayıcı:model yaz.")
        scope = [str(s) for s in spec.get("scope") or [] if str(s).strip()]
        m = Member(name=name, task=task, constraints=str(spec.get("constraints") or "").strip(), scope=scope,
                   model=model, thinking=think, isolation=iso,
                   max_steps=max(5, min(int(spec.get("max_steps") or 60), 200)))
        m.cwd = self._workdir(m)
        self.members[name] = m
        self.inbox[name] = []
        return m

    def _workdir(self, m: Member) -> Path:
        base = self.cwd
        if m.isolation != "worktree":
            return base
        try:
            top = Path(subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"], capture_output=True,
                                      text=True, check=True, timeout=20).stdout.strip())
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"{m.name}: worktree için çalışma klasörü bir git deposu olmalı ({e})") from e
        path = top / ".alice-team" / m.name
        m.branch = f"alice-team/{m.name}-{time.strftime('%m%d-%H%M%S')}"
        r = subprocess.run(["git", "-C", str(top), "worktree", "add", "-b", m.branch, str(path), "HEAD"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ToolError(f"{m.name}: worktree açılamadı: {(r.stderr or r.stdout).strip()[:300]}")
        try:  # .alice-team git status'u kirletmesin
            excl = top / ".git" / "info" / "exclude"
            old = excl.read_text(encoding="utf-8", errors="replace") if excl.exists() else ""
            if excl.parent.is_dir() and ".alice-team/" not in old:
                with excl.open("a", encoding="utf-8") as f:
                    f.write("\n.alice-team/\n")
        except OSError:
            pass
        rel = base.resolve().relative_to(top.resolve()) if base.resolve() != top.resolve() else Path()
        return path / rel

    def start(self) -> None:
        for m in self.members.values():
            self._launch(m)

    def _running(self) -> int:
        return sum(1 for m in self.members.values() if m.status in ("running", "waiting"))

    def _launch(self, m: Member, resume: bool = False) -> None:
        with self.cond:
            if self.stopped or m.status in ("running", "waiting"):
                return
            if self._running() >= self.max_parallel:
                if resume:
                    m.resume_wanted = True
                else:
                    m.status = "queued"
                return
            m.status = "running"
            m.resume_wanted = m.reported = False
            m.started = m.started or time.time()
            m.ended = 0.0
        m.thread = threading.Thread(target=self._run_member, args=(m, resume), daemon=True, name=f"team-{m.name}")
        m.thread.start()
        self._changed()

    def _next(self) -> None:
        for m in self.members.values():
            if self._running() >= self.max_parallel:
                return
            if m.status == "queued":
                self._launch(m)
            elif m.resume_wanted and m.status in DONE_STATES:
                self._launch(m, resume=True)

    def _run_member(self, m: Member, resume: bool) -> None:
        final = "failed"
        try:
            if m.agent is None:
                m.agent = self._make_agent(m)
            if resume:
                msgs = self.take(m.name)
                prompt = "[takım mesajları]\n" + "\n".join(x.line() for x in msgs) + \
                    "\n\nBunlara göre işine devam et; bitince yine kısa rapor yaz."
            else:
                prompt = "Görevine başla."
            text = m.agent.run_turn(prompt, max_steps=m.max_steps) or ""
            final = "stopped" if self.stopped or m.agent.cancel.is_set() else "done"
            m.result = text.strip() or m.result or "(rapor yazmadı)"
        except Exception as e:  # noqa: BLE001 - bir üyenin çökmesi takımı durdurmasın
            m.result = f"hata: {type(e).__name__}: {e}"
        finally:
            # Bitiş, kayıt diske yazıldıktan sonra görünsün: bekleyen orkestratör ve Projeler listesi tutarlı olsun.
            with self.cond:
                m.status, m.ended = final, time.time()
                self.save()
                self.cond.notify_all()
            self._emit(f"{m.name} {'bitti' if final == 'done' else final}")
            self._next()
            self._changed()

    def _make_agent(self, m: Member):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.parent_ctx.state.get("agent")
        if parent is None or self.parent_ctx.router is None:
            raise RuntimeError("takım bu bağlamda kullanılamıyor")
        ctx = ToolContext(cwd=m.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s, _n=m.name: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [TeamSend(self, m.name),
                                                                            TeamWait(self, m.name)]
        perms = Permissions("plan" if m.isolation == "readonly" else parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, m), parent.decider,
                      system_prompt=build_system_prompt(m.cwd, personality=False) + "\n\n" + self.brief(m),
                      is_child=True)
        agent.stream = False
        role = f"team:{m.name}"
        router = self.parent_ctx.router
        router.role_overrides[role] = list(router.chain(m.model))
        if m.thinking != "default":
            router.thinking_overrides[role] = m.thinking
        self.roles.append(role)
        agent.role = role
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]  # gruplar yok: hepsi açık
        ctx.state["agent"] = agent  # uzun araçlar Durdur'u görsün
        scope = [ctx.resolve(s) for s in m.scope]
        base_run = agent._run_tool

        def guarded(tool: Tool, args: dict[str, Any]) -> ToolResult:
            if tool.name in WRITE_TOOLS and scope:
                bad = [p for p in write_targets(tool.name, args) if not in_scope(ctx.resolve(p), scope)]
                if bad:
                    return ToolResult(f"kapsam dışı: {', '.join(bad)}. Sadece şuralara yazabilirsin: "
                                      f"{', '.join(m.scope)}. Başka bir yerin değişmesi gerekiyorsa sahibine ya da "
                                      f"{ORCHESTRATOR}'e team_send ile yaz.", is_error=True)
            m.steps += 1
            m.last = f"{tool.name} {tool.summary(args)[:90]}"
            self._changed()
            res = base_run(tool, args)
            if tool.name in WRITE_TOOLS and not res.is_error:
                for t in write_targets(tool.name, args):
                    rel = _rel(ctx.resolve(t), self.cwd)
                    if rel not in m.files:
                        m.files.append(rel)
            return res

        agent._run_tool = guarded

        def deliver() -> None:  # her araç adımından sonra gelen mesajları konuşmaya ekle
            msgs = self.take(m.name)
            if msgs:
                agent.messages.append({"role": "user", "content": "[takım mesajı]\n" +
                                       "\n".join(x.line() for x in msgs)})

        agent.on_step = deliver
        if m.saved_messages:  # diskten yüklenmiş proje: konuşmaya kaldığı yerden devam
            agent.messages = agent.messages[:1] + m.saved_messages
            m.saved_messages = []
        return agent

    def brief(self, m: Member) -> str:
        roster = "\n".join(f"- {o.name}: {o.task[:160]}" + (f" (yazar: {', '.join(o.scope)})" if o.scope else "")
                           for o in self.members.values() if o.name != m.name) or "- (tek başınasın)"
        where = {"shared": "Diğerleriyle aynı klasörde çalışıyorsun; başkasının dosyasına dokunma.",
                 "worktree": f"Kendi git worktree'ndesin (dal {m.branch}); değişikliklerini bu dalda commit et, "
                             "ALICE sonra birleştirecek.",
                 "readonly": "Salt okuma: dosya değiştiremez, değiştiren komut çalıştıramazsın; incele ve raporla."}
        return f"""# Takım görevi
Sen ALICE'in kurduğu bir takımın "{m.name}" adlı üyesisin. Büyük bir işin bir parçasını yapıyorsun; kullanıcıyla değil
ALICE'le (orkestratör, adı "{ORCHESTRATOR}") ve takım arkadaşlarınla konuşursun.

Genel hedef: {self.goal}

Senin görevin: {m.task}
Sınırlar: {m.constraints or "özel bir sınır yok; yine de sadece kendi görevini yap."}
Yazma kapsamın: {", ".join(m.scope) if m.scope else "tüm çalışma klasörü (ama başkalarının alanına girme)"}
Çalışma klasörü: {m.cwd}. {where[m.isolation]}

Takım arkadaşların:
{roster}

{"Bu projeyi kullanıcı kendisi kurdu ve yönetiyor: '" + ORCHESTRATOR + "'e yazdıkların ona gider." if self.origin == "user" else ""}
Kurallar:
- Sadece kendi görevini yap. Başkasının alanındaki bir şeyin değişmesi gerekiyorsa ona team_send ile yaz.
- Başkalarını etkileyen bir arayüz/sözleşme (fonksiyon adı, API, veri biçimi) belirlediğinde erkenden ilgili kişiye
  ya da to="all" ile herkese haber ver.
- Karar gerekiyorsa, takıldıysan ya da kapsam dışına çıkman gerekiyorsa {ORCHESTRATOR}'e sor ve team_wait ile cevabı
  bekle. Gereksiz mesaj atma; kısa ve net yaz.
- Sana gelen mesajlar konuşmana "[takım mesajı]" olarak eklenir.
- İşini doğrula (test, çalıştırma). Bitince son mesajında kısa rapor yaz: ne yaptın, hangi dosyalar değişti,
  nasıl doğruladın, açık kalan ne.
"""

    # ----------------------------------------------------------------- durum
    def status_text(self) -> str:
        icon = {"queued": "…", "running": "▶", "waiting": "⏸", "done": "✓", "failed": "✗", "stopped": "■"}
        rows = []
        for m in self.members.values():
            s = m.snapshot()
            rows.append(f"{icon.get(m.status, '?')} {m.name} [{m.status}, {m.model}"
                        f"{', düşünme ' + m.thinking if m.thinking != 'default' else ''}, {m.steps} adım, {s['secs']} sn]"
                        + (f" dal={m.branch}" if m.branch else "") + (f" son: {m.last}" if m.last else ""))
        return "\n".join(rows)

    def snapshot(self) -> dict[str, Any]:
        return {"type": "team", "id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin,
                "cwd": str(self.cwd), "created": self.created,
                "members": [m.snapshot() for m in self.members.values()],
                "log": [{"from": x.sender, "to": x.to, "text": x.text[:400], "at": x.at} for x in self.log[-40:]],
                "active": self.active()}

    def active(self) -> bool:
        return not self.all_done() and not self.stopped

    def _changed(self, save: bool = False) -> None:
        try:
            self.publish(self.snapshot())
        except Exception:  # noqa: BLE001
            pass
        if save or time.monotonic() - self._saved_at > SAVE_EVERY:
            self.save()

    # ---------------------------------------------------------------- kayıt
    def path(self) -> Path:
        return projects_dir() / f"{self.id}.json"

    def save(self) -> None:
        self._saved_at = time.monotonic()
        members = []
        for m in self.members.values():
            d = m.snapshot()
            d.update(result=m.result, max_steps=m.max_steps, cwd=str(m.cwd), started=m.started, ended=m.ended,
                     messages=_slim(m.messages()))
            members.append(d)
        body = {"id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin, "cwd": str(self.cwd),
                "created": self.created, "updated": time.time(), "members": members,
                "log": [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in self.log[-500:]]}
        try:
            with self._save_lock:  # üyeler aynı anda kaydetmeye çalışabilir
                tmp = self.path().with_suffix(".json.tmp")
                tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.path())
        except (OSError, TypeError, ValueError):
            pass

    @classmethod
    def load(cls, project_id: str, parent_ctx: ToolContext, max_parallel: int = 5,
             publish: Callable[[dict[str, Any]], None] | None = None) -> "Team":
        f = projects_dir() / f"{Path(project_id).name}.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        team = cls(parent_ctx, data.get("goal", ""), max_parallel, publish, origin=data.get("origin", "alice"),
                   project_id=data.get("id") or f.stem, title=data.get("title", ""))
        team.created = float(data.get("created") or time.time())
        if data.get("cwd"):
            team.cwd = Path(data["cwd"])
        for d in data.get("members") or []:
            m = Member(name=d["name"], task=d.get("task", ""), constraints=d.get("constraints", ""),
                       scope=list(d.get("scope") or []), model=d.get("model", "brain"),
                       thinking=d.get("thinking", "default"), isolation=d.get("isolation", "shared"),
                       max_steps=int(d.get("max_steps") or 60), cwd=Path(d.get("cwd") or team.cwd),
                       branch=d.get("branch", ""), result=d.get("result", ""), steps=int(d.get("steps") or 0),
                       last=d.get("last", ""), started=float(d.get("started") or 0), ended=float(d.get("ended") or 0),
                       files=list(d.get("files") or []), saved_messages=list(d.get("messages") or []))
            # Kapanırken yarıda kalanlar durmuş sayılır; bitmiş/hatalılar olduğu gibi, hepsi bildirilmiş.
            m.status = d.get("status") if d.get("status") in ("done", "failed") else "stopped"
            m.ended = m.ended or team.created
            m.reported = True
            team.members[m.name] = m
            team.inbox[m.name] = []
        team.log = [Message(x.get("from", "?"), x.get("to", "?"), x.get("text", ""), float(x.get("at") or 0))
                    for x in data.get("log") or []]
        return team

    def member_view(self, name: str) -> dict[str, Any]:
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"takımda '{name}' yok")
        return {**m.snapshot(), "messages": m.messages()}

    def add_member(self, spec: dict[str, Any], announce: bool = True) -> Member:
        """Çalışan ya da bitmiş projeye yeni üye ekle ve başlat; diğerlerine haber ver."""
        m = self.add(spec)
        self.stopped = False
        if announce and len(self.members) > 1:
            others = [n for n in self.members if n != m.name]
            note = f"takıma yeni üye katıldı: {m.name}: {m.task[:200]}" + (f" (yazar: {', '.join(m.scope)})" if m.scope else "")
            with self.cond:
                msg = Message("sistem", "all", note)
                self.log.append(msg)
                for n in others:  # bitmiş üyeleri uyandırmadan sadece haber ver
                    self.inbox[n].append(msg)
        self._launch(m)
        self._changed(save=True)
        return m

    def _emit(self, text: str) -> None:
        try:
            self.parent_ctx.emit(f"takım: {text}")
        except Exception:  # noqa: BLE001
            pass

    def stop(self, name: str | None = None) -> None:
        targets = [self.members[name]] if name else list(self.members.values())
        if not name:
            self.stopped = True
        for m in targets:
            if m.status == "queued":
                m.status = "stopped"
            m.resume_wanted = False
            if m.agent is not None:
                m.agent.stop()
        with self.cond:
            self.cond.notify_all()
        self._changed(save=True)

    def cleanup(self) -> None:
        router = self.parent_ctx.router
        for role in self.roles:
            router.role_overrides.pop(role, None)
            router.thinking_overrides.pop(role, None)
        self.roles.clear()


class MemberUI:
    """Üyenin arayüzü: olayları takım durumuna yansıtır, izin sorularını üyenin adıyla kullanıcıya iletir."""

    def __init__(self, team: Team, member: Member):
        self.team, self.member = team, member

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.member.last = f"{name} hata: {result.text[:90]}"
            self.team._changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        parent = self.team.parent_ctx.state.get("agent")
        ask = getattr(getattr(parent, "ui", None), "ask_permission", None)
        if ask is None:
            return "n"
        self.member.status = "waiting"
        self.team._changed()
        try:
            with self.team.perm_lock:
                ans = ask(name, f"[{self.member.name}] {summary}", risk)
        finally:
            self.member.status = "running"
            self.team._changed()
        return "y" if ans == "a" else ans  # "her zaman" tek üyeye değil, ana oturuma ait kalsın


class TeamSend(Tool):
    name = "team_send"
    description = ("Takım arkadaşına ya da orkestratöre (ALICE) mesaj gönderir. Arayüz/sözleşme duyurmak, karar "
                   "sormak, başkasının alanında bir değişiklik istemek için. Kısa ve net yaz.")
    parameters = obj({
        "to": {"type": "string", "description": f"Üye adı, '{ORCHESTRATOR}' (orkestratör) ya da 'all' (herkes)."},
        "text": {"type": "string", "description": "Mesaj."},
    }, ["to", "text"])
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"

    def run(self, ctx: ToolContext, to: str, text: str) -> ToolResult:
        return ToolResult(self.team.send(self.me, str(to).strip().lower(), text))


class TeamWait(Tool):
    name = "team_wait"
    description = ("Sana mesaj gelene kadar bekler (en çok `seconds` saniye) ve gelenleri döndürür. Bir soruya cevap "
                   "beklerken ya da başka bir üyenin işine bağlıyken kullan; boşuna bekleme.")
    parameters = obj({"seconds": {"type": "integer", "description": "En çok kaç saniye. Varsayılan 120, en çok 600."}})
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me

    def run(self, ctx: ToolContext, seconds: int = 120) -> ToolResult:
        m = self.team.members[self.me]
        m.status = "waiting"
        self.team._changed()
        try:
            msgs = self.team.wait_messages(self.me, max(1, min(int(seconds or 120), 600)), ctx.cancelled)
        finally:
            m.status = "running"
            self.team._changed()
        if not msgs:
            return ToolResult("süre doldu, mesaj gelmedi. Bekleyebileceğin başka bir şey yoksa işine devam et.")
        return ToolResult("\n".join(x.line() for x in msgs))
