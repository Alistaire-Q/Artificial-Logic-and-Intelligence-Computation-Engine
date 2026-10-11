"""Projeler: büyük bir işi paralel çalışan ajanlarla, kendi yöneticisi olan otonom bir proje olarak yürütmek.

Her projenin bir yöneticisi (lead, adı "alice") ve işçi ajanları (üyeler) vardır. Hepsi kendi thread'inde, kendi
bağlamıyla çalışan alt ajanlardır.

Yönetici sohbetten bağımsız çalışır ve olay geldikçe uyanır:
- Plan: ajan verilmemişse kodu inceleyip işi parçalara böler, `assign` ile ajanları kurar.
- İzleme: ajanların sorularını `message` ile cevaplar; kullanıcıya bağlı kararları `ask_user` ile sorar.
- İnceleme: biten her ajan "review" durumuna geçer; yönetici raporu ve farkı (`diff`) okur, gerekirse test çalıştırır,
  `review` ile onaylar (worktree dalı birleştirilir) ya da geri bildirimle geri gönderir.
- Bitiş: herkes onaylanınca bütünü doğrular ve `finish` ile kullanıcıya özet yazar.

Üyeler `team_send` ile birbirine, yöneticiye ("alice") ya da herkese yazar, `team_wait` ile cevap bekler; gelen mesajlar
her araç adımından sonra konuşmalarına eklenir. `after` ile bir üye başkalarının işi onaylanınca başlar.

Her proje ~/.alice/projects/<id>.json'a kaydedilir; uygulama kapanıp açılsa da yüklenir, kullanıcı yazınca kaldığı
yerden devam eder. Uygulamanın Projeler sayfası durumları, konuşmaları, farkları ve soruları gösterir.

Güvenlik: ajanlar sadece kod işi araçlarını kullanır (dosya, kabuk, web, git); ekran/tarayıcı araçları kullanıcının
tek ekranını paylaştığı için kapalı. Yazma kapsamı verilen ajan kapsam dışına dosya araçlarıyla yazamaz; "readonly"
ajan hiç yazamaz. İzin soruları ajanın adıyla kullanıcıya gider.
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

from alice.tools.base import READ, Tool, ToolContext, ToolError, ToolResult, make_diff, obj

LEAD = "alice"  # projenin yöneticisi
ORCHESTRATOR = LEAD  # eski ad
USER = "kullanıcı"  # Projeler sayfasından yazan kullanıcı
SYSTEM = "sistem"
# Ajanların kullanabildiği araçlar: kod işi. Ekran, tarayıcı, masaüstü araçları kullanıcının tek ekranını paylaşır.
MEMBER_TOOLS = {"read_file", "write_file", "edit_file", "multi_edit", "apply_patch", "file_ops", "list_dir", "glob",
                "grep", "run_command", "process_output", "process_stop", "web_fetch", "web_search", "http_request",
                "todo_write", "git"}
WRITE_TOOLS = {"write_file", "edit_file", "multi_edit", "apply_patch", "file_ops"}
ISOLATIONS = ("shared", "worktree", "readonly")
THINKING = ("off", "low", "medium", "high", "default")
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
ACTIVE = ("running", "waiting", "asking")
FINAL = ("done", "failed", "stopped")
SAVE_EVERY = 1.5  # saniye: canlı güncellemelerde diske en sık bu aralıkla yazılır
MAX_MEMBERS = 8
ORIGINAL_LIMIT = 400_000  # geri alma/fark için saklanan dosya ilk hali (bayt); daha büyükleri saklanmaz


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
    after: list[str] = field(default_factory=list)  # bunlar onaylanınca başlar
    cwd: Path = Path(".")
    branch: str = ""
    base_sha: str = ""  # worktree açıldığı commit
    status: str = "queued"  # queued | running | waiting | asking | review | done | failed | stopped
    result: str = ""
    question: str = ""  # yöneticiye/kullanıcıya sorup cevap beklediği soru
    steps: int = 0
    reviews: int = 0  # kaç kez düzeltmeye gönderildi
    limit_hit: bool = False
    last: str = ""
    started: float = 0.0
    ended: float = 0.0
    files: list[str] = field(default_factory=list)  # dosya araçlarıyla değiştirdiği yollar (proje klasörüne göre)
    originals: dict[str, str | None] = field(default_factory=dict)  # mutlak yol -> ilk hali (None: yoktu)
    agent: Any = None
    thread: threading.Thread | None = None
    saved_messages: list[dict[str, Any]] = field(default_factory=list)  # diskten yüklenmiş konuşma (sistem hariç)
    resume_wanted: bool = False  # bitmişken iş geldi ama paralel sınırı dolu: yer açılınca devam edecek

    def snapshot(self) -> dict[str, Any]:
        now = self.ended or time.time()
        return {"name": self.name, "task": self.task, "model": self.model, "thinking": self.thinking,
                "isolation": self.isolation, "status": self.status, "steps": self.steps, "last": self.last,
                "branch": self.branch, "scope": self.scope, "result": self.result[:1500], "files": self.files[-50:],
                "constraints": self.constraints, "after": self.after, "question": self.question,
                "reviews": self.reviews, "limit_hit": self.limit_hit,
                "secs": int(now - self.started) if self.started else 0}

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
    """Dosya aracının yazacağı yollar (kapsam kontrolü ve fark için)."""
    if tool == "apply_patch":
        return patch_paths(str(args.get("patch") or ""))
    out = [str(args[k]) for k in ("path", "destination") if args.get(k)]
    out += [str(p) for p in args.get("paths") or []]
    return out


def _read_original(p: Path) -> str | None:
    if not p.exists() or p.is_dir():
        return None
    try:
        if p.stat().st_size > ORIGINAL_LIMIT:
            return "\0büyük"
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "\0okunamadı"


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)


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
        status = data.get("status") or "stopped"
        out.append({"id": data.get("id") or f.stem, "title": data.get("title") or "", "goal": data.get("goal", ""),
                    "created": data.get("created", 0), "updated": data.get("updated", 0),
                    "origin": data.get("origin", "alice"), "cwd": data.get("cwd", ""),
                    "status": status if status in ("done", "stopped") else "stopped",
                    "questions": sum(1 for q in data.get("questions") or [] if not q.get("answer")),
                    "members": [{"name": m.get("name"), "status": m.get("status")} for m in members]})
    return sorted(out, key=lambda x: -float(x.get("updated") or x.get("created") or 0))


class Team:
    """Bir proje: hedef, yönetici ve ajanlar."""

    def __init__(self, parent_ctx: ToolContext, goal: str, max_parallel: int = 5,
                 publish: Callable[[dict[str, Any]], None] | None = None, origin: str = "alice",
                 project_id: str = "", title: str = "", review: bool = True, lead_model: str = "brain",
                 lead_thinking: str = "default", cwd: Path | None = None):
        self.parent_ctx = parent_ctx
        self.goal = goal
        self.id = project_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.title = title or (goal.strip().splitlines()[0][:70] if goal.strip() else "Proje")
        self.origin = origin  # alice: sohbetteki ALICE kurdu | user: kullanıcı Projeler sayfasından kurdu
        self.cwd = Path(cwd) if cwd else parent_ctx.cwd
        self.created = time.time()
        self.max_parallel = max(1, max_parallel)
        self.review = review  # biten işi yönetici incelesin mi
        self.lead_model = lead_model
        self.lead_thinking = lead_thinking if lead_thinking in THINKING else "default"
        self.members: dict[str, Member] = {}
        self.log: list[Message] = []
        self.inbox: dict[str, list[Message]] = {LEAD: []}
        self.questions: list[dict[str, Any]] = []  # yöneticinin kullanıcıya soruları
        self.summary = ""
        self.finished = False
        self.stopped = False
        self.lead_agent: Any = None
        self.lead_thread: threading.Thread | None = None
        self.lead_state = "idle"  # idle | running | done
        self.lead_last = ""
        self.lead_saved: list[dict[str, Any]] = []
        self._nudges = 0
        self.cond = threading.Condition()
        self.perm_lock = threading.Lock()  # izin pencereleri tek tek açılsın
        self.publish = publish or (lambda ev: None)
        self._live_ui = publish is not None  # uygulamada: bildirimler tıklanabilir proje kartı olarak gider
        self.roles: list[str] = []
        self._saved_at = 0.0
        self._save_lock = threading.Lock()

    # --------------------------------------------------------------- durum
    def status(self) -> str:
        if self.finished:
            return "done"
        if self.stopped:
            return "stopped"
        if any(not q.get("answer") for q in self.questions):
            return "needs_you"
        ms = list(self.members.values())
        if not ms:
            return "planning"
        if any(m.status in ACTIVE or m.status == "queued" for m in ms) or self.lead_state == "running":
            return "running"
        return "review"

    def active(self) -> bool:
        return not self.finished and not self.stopped

    def all_done(self) -> bool:
        """Ajanların hepsi son durumda (yönetici bitirmemiş olabilir)."""
        return bool(self.members) and all(m.status in FINAL and not m.resume_wanted for m in self.members.values())

    # ------------------------------------------------------------- mesajlar
    def send(self, sender: str, to: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            raise ToolError("mesaj boş")
        to = (to or "").strip().lower()
        if to == "all":
            targets = [n for n in self.members if n != sender] + ([LEAD] if sender != LEAD else [])
        else:
            targets = [to]
        unknown = [t for t in targets if t != LEAD and t not in self.members]
        if unknown:
            raise ToolError(f"projede böyle biri yok: {', '.join(unknown)}. Ajanlar: {', '.join(self.members) or '-'}, "
                            f"yönetici: {LEAD}")
        boss = sender in (LEAD, USER)
        if sender == USER:
            self.stopped = False
            if LEAD in targets:
                self.finished = False  # bitmiş projeye kullanıcı yeni iş verdi
        resume = []
        with self.cond:
            msg = Message(sender, to, text)
            self.log.append(msg)
            for t in targets:
                self.inbox.setdefault(t, []).append(msg)
                m = self.members.get(t)
                if m is None:
                    continue
                if boss:
                    m.question = ""  # yönetici/kullanıcı cevap verdi
                if self.stopped:
                    continue
                if m.status in ("done", "failed") or (m.status == "review" and boss) or \
                        (m.status == "stopped" and sender == USER):
                    resume.append(m)
            if sender in self.members and to in (LEAD, USER):
                self.members[sender].question = text  # cevap beklenen soru
            self.cond.notify_all()
        for m in resume:  # bitmiş ajana iş geldi: konuşmasına kaldığı yerden devam etsin
            self._launch(m, resume=True)
        if LEAD in targets and sender != LEAD:
            self._ensure_lead()
        self._changed()
        return f"gönderildi: {', '.join(targets)}"

    def event(self, text: str) -> None:
        """Yöneticiye sistem olayı (ajan bitti, hata verdi...)."""
        with self.cond:
            self.inbox[LEAD].append(Message(SYSTEM, LEAD, text))
            self.cond.notify_all()
        self._ensure_lead()

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

    def wait_until(self, pred: Callable[[], bool], timeout: float, cancelled: Callable[[], bool] = lambda: False) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not pred():
                left = end - time.monotonic()
                if left <= 0 or cancelled():
                    return False
                self.cond.wait(min(left, 0.5))
            return True

    def ask_user(self, question: str) -> dict[str, Any]:
        q = {"id": secrets.token_hex(3), "text": question.strip(), "at": time.time(), "answer": ""}
        with self.cond:
            self.questions.append(q)
            self.log.append(Message(LEAD, USER, question.strip()))
            self.cond.notify_all()
        self._emit(f"'{self.title}' projesi sana soruyor: {question.strip()[:160]}", kind="ask")
        self._changed(save=True)
        return q

    def answer(self, qid: str, text: str) -> None:
        q = next((q for q in self.questions if q["id"] == qid), None)
        if q is None:
            raise ToolError("soru bulunamadı")
        if not (text or "").strip():
            raise ToolError("cevap boş")
        q["answer"] = text.strip()
        self.send(USER, LEAD, f"Sorduğun soruya cevap ({q['text'][:200]}): {text.strip()}")

    # --------------------------------------------------------------- üyeler
    def add(self, spec: dict[str, Any]) -> Member:
        name = str(spec.get("name") or "").strip().lower()
        if not NAME_RX.match(name) or name in (LEAD, "all", USER, SYSTEM):
            raise ToolError(f"geçersiz ajan adı '{name}': küçük harf/rakam/-/_, en çok 24 karakter; 'alice' ve 'all' "
                            "ayrılmış")
        if name in self.members:
            raise ToolError(f"'{name}' adı zaten var")
        if len(self.members) >= MAX_MEMBERS:
            raise ToolError(f"bir projede en çok {MAX_MEMBERS} ajan olur")
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
        self._check_model(model, name)
        after = [str(a).strip().lower() for a in spec.get("after") or [] if str(a).strip()]
        bad = [a for a in after if a not in self.members]
        if bad:
            raise ToolError(f"{name}: after içinde olmayan ajan: {', '.join(bad)} (önce onları ekle)")
        scope = [str(s) for s in spec.get("scope") or [] if str(s).strip()]
        m = Member(name=name, task=task, constraints=str(spec.get("constraints") or "").strip(), scope=scope,
                   model=model, thinking=think, isolation=iso, after=after,
                   max_steps=max(5, min(int(spec.get("max_steps") or 60), 200)))
        m.cwd = self._workdir(m)
        self.members[name] = m
        self.inbox[name] = []
        return m

    def _check_model(self, model: str, who: str) -> None:
        router = self.parent_ctx.router
        try:
            ok = router is None or bool(router.chain(model))
        except KeyError:
            ok = False
        if not ok:
            raise ToolError(f"{who}: '{model}' için model yok. Rol adı (brain, fast...) ya da sağlayıcı:model yaz.")

    def _repo_top(self) -> Path:
        r = _git(self.cwd, "rev-parse", "--show-toplevel", timeout=20)
        if r.returncode != 0:
            raise ToolError("worktree için çalışma klasörü bir git deposu olmalı")
        return Path(r.stdout.strip())

    def _workdir(self, m: Member) -> Path:
        base = self.cwd
        if m.isolation != "worktree":
            return base
        try:
            top = self._repo_top()
        except (OSError, subprocess.SubprocessError) as e:
            raise ToolError(f"{m.name}: worktree için çalışma klasörü bir git deposu olmalı ({e})") from e
        path = top / ".alice-team" / f"{self.id}-{m.name}"
        m.branch = f"alice-team/{m.name}-{time.strftime('%m%d-%H%M%S')}"
        m.base_sha = _git(top, "rev-parse", "HEAD").stdout.strip()
        r = _git(top, "worktree", "add", "-b", m.branch, str(path), "HEAD")
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

    def start(self, plan: bool = False) -> None:
        """Projeyi başlat: ajanlar başlar, yönetici izlemeye (ya da ajan yoksa planlamaya) başlar."""
        for m in self.members.values():
            self._launch(m)
        if plan or not self.members:
            self.event("Proje başladı ve henüz ajan yok. Kodu kısaca incele, işi çakışmayan parçalara böl ve assign "
                       "ile ajanları kur. İş küçükse tek ajan yeter.")
        else:
            self.event("Proje başladı; ajanlar çalışıyor. Şimdilik yapacak bir şey yoksa turunu bitir; soru ya da "
                       "inceleme gelince uyandırılırsın.")
        self._changed(save=True)

    def _running(self) -> int:
        return sum(1 for m in self.members.values() if m.status in ACTIVE)

    def _deps_ok(self, m: Member) -> bool:
        return all(self.members[a].status == "done" for a in m.after if a in self.members)

    def _launch(self, m: Member, resume: bool = False) -> None:
        with self.cond:
            if (self.stopped or self.finished) and not resume:
                return
            if m.status in ACTIVE:
                return
            if not resume and not self._deps_ok(m):
                m.status = "queued"
                return
            if self._running() >= self.max_parallel:
                if resume:
                    m.resume_wanted = True
                else:
                    m.status = "queued"
                return
            m.status = "running"
            m.resume_wanted = False
            m.started = m.started or time.time()
            m.ended = 0.0
        m.thread = threading.Thread(target=self._run_member, args=(m, resume), daemon=True, name=f"team-{m.name}")
        m.thread.start()
        self._changed()

    def _next(self) -> None:
        for m in self.members.values():
            if self._running() >= self.max_parallel:
                return
            if m.status == "queued" and self._deps_ok(m):
                self._launch(m)
            elif m.resume_wanted and m.status not in ACTIVE:
                self._launch(m, resume=True)

    def _run_member(self, m: Member, resume: bool) -> None:
        final = "failed"
        try:
            if m.agent is None:
                m.agent = self._make_agent(m)
            if resume:
                msgs = self.take(m.name)
                prompt = ("[takım mesajları]\n" + "\n".join(x.line() for x in msgs) if msgs else "Devam et.") + \
                    "\n\nBunlara göre işine devam et; bitince yine kısa rapor yaz."
            else:
                prompt = "Görevine başla."
            m.limit_hit = False
            text = m.agent.run_turn(prompt, max_steps=m.max_steps) or ""
            if self.stopped or m.agent.cancel.is_set():
                final = "stopped"
            else:
                final = "review" if self.review and m.isolation != "readonly" else "done"
            m.result = text.strip() or m.result or "(rapor yazmadı)"
        except Exception as e:  # noqa: BLE001 - bir ajanın çökmesi projeyi durdurmasın
            m.result = f"hata: {type(e).__name__}: {e}"
        finally:
            # Bitiş, kayıt diske yazıldıktan sonra görünsün: bekleyenler ve Projeler listesi tutarlı olsun.
            with self.cond:
                m.status, m.ended, m.question = final, time.time(), ""
                self.save()
                self.cond.notify_all()
        if final == "review":
            self.event(self._review_note(m))
        elif final == "done":
            self.event(f"{m.name} bitirdi (inceleme yok). Raporu:\n{m.result[:3000]}")
            self._after_done(m)
        elif final == "failed":
            self.event(f"{m.name} hata verdi: {m.result[:600]}. Gerekirse message ile yeniden dene ya da işi "
                       "başka ajana ver.")
        self._next()
        self._changed()

    def _review_note(self, m: Member) -> str:
        files = ", ".join(m.files[-30:]) or "dosya değiştirmedi"
        extra = " Adım limiti doldu; iş yarım kalmış olabilir." if m.limit_hit else ""
        return (f"{m.name} bitirdi, inceleme bekliyor.{extra}\nRaporu:\n{m.result[:3000]}\n\nDeğişen dosyalar: {files}"
                + (f"\nDal: {m.branch} (onaylayınca birleştirilir)" if m.branch else "") +
                "\n\nİncele: diff ile farka bak, gerekirse testleri çalıştır; sonra review ile onayla ya da "
                "düzeltme iste.")

    def _after_done(self, m: Member) -> None:
        self._next()  # bu ajanı bekleyenler başlasın
        if self.all_done() and not self.finished:
            self.event("Bütün ajanlar bitti. Bütünü doğrula (testler, derleme), gerekirse küçük entegrasyon "
                       "düzeltmesini yap, sonra finish ile kullanıcıya özet yaz.")

    # -------------------------------------------------------------- inceleme
    def review_member(self, name: str, approve: bool, feedback: str = "", by: str = LEAD) -> str:
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if approve:
            if m.status not in ("review", "done", "failed", "stopped"):
                raise ToolError(f"{name} şu an {m.status}; bitince onaylanabilir")
            note = self._merge(m) if m.branch else ""
            m.status = "done"
            with self.cond:
                self.log.append(Message(by, name, "onaylandı" + (f": {feedback.strip()}" if feedback.strip() else "")))
                self.cond.notify_all()
            if by != LEAD:
                self.event(f"Kullanıcı {name} ajanının işini onayladı.")
            self._after_done(m)
            self._changed(save=True)
            return f"{name} onaylandı" + (f"; {note}" if note else "")
        if not feedback.strip():
            raise ToolError("düzeltme için feedback yaz: neyin yanlış olduğunu ve ne beklediğini söyle")
        m.reviews += 1
        self.send(by, name, "İnceleme sonucu düzeltme gerekli:\n" + feedback.strip())
        return f"{name} düzeltmeye gönderildi"

    def _merge(self, m: Member) -> str:
        """Worktree ajanının dalını ana çalışma klasörüne birleştir (önce kalan değişiklikleri commit eder)."""
        top = self._repo_top()
        wt = m.cwd
        ident = ["-c", "user.name=ALICE", "-c", "user.email=alice@localhost"]
        if _git(wt, "status", "--porcelain").stdout.strip():
            _git(wt, "add", "-A")
            r = _git(wt, *ident, "commit", "-q", "-m", f"alice-team {m.name}: {m.task[:60]}")
            if r.returncode != 0:
                raise ToolError(f"{m.name} dalında commit yapılamadı: {(r.stderr or r.stdout).strip()[:300]}")
        if _git(top, "merge-base", "--is-ancestor", m.branch, "HEAD").returncode == 0:
            return "dalda birleştirilecek yeni bir şey yok"
        r = _git(top, *ident, "merge", "--no-ff", "--no-edit", m.branch)
        if r.returncode != 0:
            _git(top, "merge", "--abort")
            raise ToolError(f"{m.branch} birleştirilemedi (çakışma ya da kirli çalışma klasörü): "
                            f"{(r.stdout + r.stderr).strip()[:500]}. Ajandan dalını güncellemesini iste ya da "
                            "kendin çöz.")
        return f"{m.branch} birleştirildi"

    def diff(self, name: str, limit: int = 60_000) -> str:
        """Ajanın dosya araçlarıyla yaptığı değişikliklerin farkı."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        parts = []
        for path, before in m.originals.items():
            p = Path(path)
            rel = _rel(p, m.cwd)
            after = _read_original(p)
            if (before or "").startswith("\0") or (after or "").startswith("\0"):
                parts.append(f"--- {rel}: dosya çok büyük ya da okunamadı, fark gösterilemiyor")
                continue
            if before == after:
                continue
            if after is None:
                parts.append(f"--- a/{rel}\n+++ /dev/null\n@@ dosya silindi @@")
                continue
            parts.append(make_diff(rel, before or "", after, max_lines=800))
        text = "\n".join(x for x in parts if x)
        return text[:limit] + ("\n... (kısaltıldı)" if len(text) > limit else "")

    def revert(self, name: str) -> list[str]:
        """Ajanın dosya araçlarıyla yaptığı değişiklikleri geri al."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(f"{name} çalışıyor; önce durdur")
        done = []
        for path, before in m.originals.items():
            p = Path(path)
            if (before or "").startswith("\0"):
                continue
            try:
                if before is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(before, encoding="utf-8")
                done.append(_rel(p, self.cwd))
            except OSError:
                pass
        m.originals.clear()
        m.files.clear()
        with self.cond:
            self.log.append(Message(USER, name, f"değişiklikleri geri alındı ({len(done)} dosya)"))
        self._changed(save=True)
        return done

    def restart(self, name: str) -> None:
        """Ajanı temiz bir konuşmayla baştan başlat."""
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        if m.status in ACTIVE:
            raise ToolError(f"{name} çalışıyor; önce durdur")
        m.agent, m.saved_messages, m.result, m.steps, m.last = None, [], "", 0, ""
        m.started, m.ended, m.status = 0.0, 0.0, "queued"
        self.stopped = self.finished = False
        self._launch(m)
        self._changed(save=True)

    def finish(self, summary: str) -> None:
        self.summary = summary.strip()
        self.finished = True
        self.lead_state = "done"
        with self.cond:
            self.log.append(Message(LEAD, USER, "Proje bitti.\n" + self.summary))
            self.cond.notify_all()
        self._emit(f"'{self.title}' projesi bitti: {self.summary.splitlines()[0][:200] if self.summary else ''}",
                   kind="done")
        self._changed(save=True)

    # --------------------------------------------------------------- ajanlar
    def _role(self, name: str, model: str, thinking: str) -> str:
        role = f"team:{self.id}:{name}"
        router = self.parent_ctx.router
        router.role_overrides[role] = list(router.chain(model))
        if thinking != "default":
            router.thinking_overrides[role] = thinking
        if role not in self.roles:
            self.roles.append(role)
        return role

    def _make_agent(self, m: Member):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.parent_ctx.state.get("agent")
        if parent is None or self.parent_ctx.router is None:
            raise RuntimeError("proje bu bağlamda çalışamıyor")
        ctx = ToolContext(cwd=m.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [TeamSend(self, m.name),
                                                                            TeamWait(self, m.name)]
        perms = Permissions("plan" if m.isolation == "readonly" else parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, m), parent.decider,
                      system_prompt=build_system_prompt(m.cwd, personality=False) + "\n\n" + self.brief(m),
                      is_child=True)
        agent.stream = False
        agent.role = self._role(m.name, m.model, m.thinking)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]  # gruplar yok: hepsi açık
        ctx.state["agent"] = agent  # uzun araçlar Durdur'u görsün
        scope = [ctx.resolve(s) for s in m.scope]
        base_run = agent._run_tool

        def guarded(tool: Tool, args: dict[str, Any]) -> ToolResult:
            targets = [ctx.resolve(p) for p in write_targets(tool.name, args)] if tool.name in WRITE_TOOLS else []
            if scope:
                bad = [str(p) for p in targets if not in_scope(p, scope)]
                if bad:
                    return ToolResult(f"kapsam dışı: {', '.join(bad)}. Sadece şuralara yazabilirsin: "
                                      f"{', '.join(m.scope)}. Başka bir yerin değişmesi gerekiyorsa sahibine ya da "
                                      f"{LEAD}'e team_send ile yaz.", is_error=True)
            for p in targets:  # fark ve geri alma için ilk hali sakla
                m.originals.setdefault(str(p), _read_original(p))
            m.steps += 1
            m.last = f"{tool.name} {tool.summary(args)[:90]}"
            self._changed()
            res = base_run(tool, args)
            if targets and not res.is_error:
                for p in targets:
                    rel = _rel(p, self.cwd)
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
                 "worktree": f"Kendi git worktree'ndesin (dal {m.branch}); değişikliklerin bu dalda kalır, yönetici "
                             "onaylayınca birleştirilir.",
                 "readonly": "Salt okuma: dosya değiştiremez, değiştiren komut çalıştıramazsın; incele ve raporla."}
        after = f"Bu iş şunlar bittikten sonra başladı: {', '.join(m.after)}. Onların değişiklikleri hazır.\n" \
            if m.after else ""
        return f"""# Proje görevi
Sen "{self.title}" projesinin "{m.name}" adlı ajanısın. Büyük bir işin bir parçasını yapıyorsun; kullanıcıyla değil
projenin yöneticisiyle (ALICE, adı "{LEAD}") ve takım arkadaşlarınla konuşursun.

Genel hedef: {self.goal}

Senin görevin: {m.task}
Sınırlar: {m.constraints or "özel bir sınır yok; yine de sadece kendi görevini yap."}
Yazma kapsamın: {", ".join(m.scope) if m.scope else "tüm çalışma klasörü (ama başkalarının alanına girme)"}
Çalışma klasörü: {m.cwd}. {where[m.isolation]}
{after}
Takım arkadaşların:
{roster}

Kurallar:
- Sadece kendi görevini yap. Başkasının alanındaki bir şeyin değişmesi gerekiyorsa ona team_send ile yaz.
- Başkalarını etkileyen bir arayüz/sözleşme (fonksiyon adı, API, veri biçimi) belirlediğinde erkenden ilgili kişiye
  ya da to="all" ile herkese haber ver.
- Karar gerekiyorsa, takıldıysan ya da kapsam dışına çıkman gerekiyorsa {LEAD}'e sor ve team_wait ile cevabı
  bekle. Gereksiz mesaj atma; kısa ve net yaz.
- Sana gelen mesajlar konuşmana "[takım mesajı]" olarak eklenir.
- İşini doğrula (test, çalıştırma). Bitince son mesajında kısa rapor yaz: ne yaptın, hangi dosyalar değişti,
  nasıl doğruladın, açık kalan ne. Yönetici işini inceler; düzeltme isterse yine sana yazar.
"""

    # ------------------------------------------------------------- yönetici
    def _ensure_lead(self) -> None:
        """Yönetici döngüsü çalışmıyorsa başlat (gelen kutusundaki olayları işler)."""
        with self.cond:
            if self.lead_thread is not None and self.lead_thread.is_alive():
                return  # döngü çalışıyor; olay gelen kutusundan alınacak
            if self.parent_ctx.state.get("agent") is None or self.parent_ctx.router is None:
                return
            self.lead_thread = threading.Thread(target=self._lead_loop, daemon=True, name=f"lead-{self.id}")
            self.lead_thread.start()

    def _make_lead(self):
        from alice.agent import Agent
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.parent_ctx.state["agent"]
        ctx = ToolContext(cwd=self.cwd, config=self.parent_ctx.config, router=self.parent_ctx.router,
                          decider=self.parent_ctx.decider, emit=lambda s: None)
        tools = [t for n, t in parent.tools.items() if n in MEMBER_TOOLS] + [
            LeadAssign(self), TeamSend(self, LEAD), LeadReview(self), LeadDiff(self), LeadAskUser(self),
            LeadFinish(self), LeadStatus(self)]
        perms = Permissions(parent.permissions.mode)
        perms.always_allow = set(parent.permissions.always_allow)
        agent = Agent(parent.config, parent.router, tools, ctx, perms, MemberUI(self, Member(name=LEAD, task="")),
                      parent.decider,
                      system_prompt=build_system_prompt(self.cwd, personality=False) + "\n\n" + self.lead_brief(),
                      is_child=True)
        agent.stream = False
        agent.role = self._role(LEAD, self.lead_model, self.lead_thinking)
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]
        ctx.state["agent"] = agent
        if self.lead_saved:
            agent.messages = agent.messages[:1] + self.lead_saved
            self.lead_saved = []
        return agent

    def _lead_loop(self) -> None:
        try:
            if self.lead_agent is None:
                self.lead_agent = self._make_lead()
            while True:
                msgs = self.wait_messages(LEAD, 1.0, lambda: self.finished)
                if not msgs:
                    nudge = "" if self.finished or self.stopped else self._nudge()
                    if not nudge:
                        if self._lead_exit():
                            return
                        continue
                    msgs = [Message(SYSTEM, LEAD, nudge)]
                self.lead_state = "running"
                self._changed()
                prompt = "[olaylar]\n" + "\n\n".join(
                    (x.text if x.sender == SYSTEM else x.line()) for x in msgs) + "\n\n[durum]\n" + self.status_text()
                text = self.lead_agent.run_turn(prompt, max_steps=int(self.parent_ctx.config.agent.get(
                    "team_lead_steps", 40))) or ""
                self.lead_last = text.strip() or self.lead_last
                self.lead_state = "done" if self.finished else "idle"
                self._changed(save=True)
        except Exception as e:  # noqa: BLE001
            self.lead_state = "idle"
            self.lead_last = f"yönetici hata verdi: {type(e).__name__}: {e}"
            self._emit(self.lead_last)
            self._changed(save=True)

    def _lead_exit(self) -> bool:
        """Yöneticinin bekleyeceği bir şey kalmadıysa döngüden çık (yeni olay gelince _ensure_lead yeniden başlatır).
        Gelen kutusu kilit altında son kez kontrol edilir: çıkarken gelen olay kaybolmasın."""
        idle = all(m.status in FINAL + ("review",) and not m.resume_wanted for m in self.members.values())
        if not (self.stopped or self.finished or (self.members and idle and self._nudges >= 2)):
            return False
        with self.cond:
            if self.inbox.get(LEAD):
                return False
            self.lead_thread = None
            return True

    def _nudge(self) -> str:
        """Yönetici turunu bitirdi ama iş ortada kaldıysa hatırlat; iki kez olmazsa projeyi kendisi kapat."""
        if self.finished or self.stopped or not self.members:
            return ""
        if any(not q.get("answer") for q in self.questions) or self._nudges >= 3:
            return ""
        pending = [m.name for m in self.members.values() if m.status == "review"]
        if pending and all(m.status in FINAL + ("review",) for m in self.members.values()):
            self._nudges += 1
            if self._nudges <= 2:
                return f"İnceleme bekleyen ajanlar var: {', '.join(pending)}. diff ile bak ve review ile karar ver."
            return ""
        if not self.all_done():
            return ""
        self._nudges += 1
        if self._nudges <= 2:
            return ("Bütün ajanlar bitti ama proje kapanmadı. Bütünü doğrula ve finish ile özet yaz "
                    "(yapacak iş kaldıysa assign ya da message ile devam et).")
        self.finish(self.lead_last or "Ajanlar işini bitirdi; yönetici özet yazmadı. Ajan raporlarına bak.")
        return ""

    def lead_brief(self) -> str:
        review = ("Biten her ajan 'review' durumuna geçer ve sana inceleme olayı gelir." if self.review else
                  "İnceleme kapalı: biten ajanlar doğrudan 'done' olur; yine de raporlarını oku.")
        return f"""# Proje yöneticisi
Sen ALICE'sin ve "{self.title}" projesinin yöneticisisin (orkestratör, ajanlar sana "{LEAD}" diye yazar).
Proje arka planda, kullanıcının sohbetinden bağımsız yürüyor. Kullanıcıyla sadece ask_user ve finish ile konuşursun.

Hedef: {self.goal}
Çalışma klasörü: {self.cwd}

Görevlerin:
1. Plan: ajan yoksa kodu kısaca incele (glob, grep, read_file) ve işi çakışmayan parçalara bölüp assign ile ajanları
   kur (en çok {MAX_MEMBERS}). Her ajana konuşmayı görmeyen birinin anlayacağı netlikte görev, doğrulama yolu ve yazma
   kapsamı (scope) ver; kapsamlar çakışmasın. Zor tasarım/hata ayıklama: model=brain thinking=high; mekanik iş:
   model=fast thinking=low. Birbirine bağlı işlerde after kullan (ör. testler backend'den sonra). Aynı dosyalara
   dokunabilecek büyük değişikliklerde isolation=worktree. Sadece inceleme/araştırma için isolation=readonly.
2. İzle: sana olaylar "[olaylar]" mesajı olarak gelir. Ajan soru sorarsa message ile cevap ver. Kullanıcının tercihine
   bağlı, senin karar veremeyeceğin şeyleri ask_user ile sor (gereksiz soru sorma).
3. İncele: {review} Raporu oku, diff ile farka bak, gerekirse testleri çalıştır (run_command). Doğruysa
   review decision=approve; eksik ya da hatalıysa decision=revise ve somut geri bildirim (ne yanlış, ne bekliyorsun).
4. Bitir: herkes onaylanınca bütünü doğrula (testler, derleme), gerekirse küçük bir entegrasyon düzeltmesini kendin
   yap, sonra finish ile kullanıcıya özet yaz: ne yapıldı, hangi dosyalar, nasıl doğrulandı, açık kalanlar.

Kurallar:
- Ajanların işini kendin yapma; sen planla, yönlendir, incele.
- Olay yoksa turunu bitir; bekleme aracı yok, yeni olay gelince uyandırılırsın.
- Kısa ve net yaz; ajanlara gereksiz mesaj atma.
"""

    # ----------------------------------------------------------------- durum
    def status_text(self) -> str:
        icon = {"queued": "…", "running": "▶", "waiting": "⏸", "asking": "?", "review": "◎", "done": "✓",
                "failed": "✗", "stopped": "■"}
        rows = []
        for m in self.members.values():
            s = m.snapshot()
            rows.append(f"{icon.get(m.status, '?')} {m.name} [{m.status}, {m.model}"
                        f"{', düşünme ' + m.thinking if m.thinking != 'default' else ''}, {m.steps} adım, {s['secs']} sn]"
                        + (f" sonra: {','.join(m.after)}" if m.after and m.status == "queued" else "")
                        + (f" dal={m.branch}" if m.branch else "") + (f" son: {m.last}" if m.last else ""))
        open_q = [q["text"] for q in self.questions if not q.get("answer")]
        if open_q:
            rows.append("Kullanıcıya açık sorular: " + " | ".join(open_q))
        return "\n".join(rows) or "(henüz ajan yok)"

    def snapshot(self) -> dict[str, Any]:
        done = sum(1 for m in self.members.values() if m.status == "done")
        return {"type": "team", "id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin,
                "cwd": str(self.cwd), "created": self.created, "status": self.status(), "review": self.review,
                "summary": self.summary, "lead": {"state": self.lead_state, "model": self.lead_model,
                                                  "thinking": self.lead_thinking, "last": self.lead_last[:600]},
                "questions": self.questions[-20:], "progress": [done, len(self.members)],
                "members": [m.snapshot() for m in self.members.values()],
                "log": [{"from": x.sender, "to": x.to, "text": x.text[:400], "at": x.at} for x in self.log[-40:]],
                "active": self.active()}

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
                     base_sha=m.base_sha, messages=_slim(m.messages()),
                     originals={k: v for k, v in m.originals.items() if v is None or len(v) < 200_000})
            members.append(d)
        lead_msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
        body = {"id": self.id, "title": self.title, "goal": self.goal, "origin": self.origin, "cwd": str(self.cwd),
                "created": self.created, "updated": time.time(), "status": self.status(), "review": self.review,
                "lead_model": self.lead_model, "lead_thinking": self.lead_thinking, "summary": self.summary,
                "finished": self.finished, "lead_last": self.lead_last, "lead_messages": _slim(lead_msgs),
                "questions": self.questions, "members": members,
                "log": [{"from": x.sender, "to": x.to, "text": x.text, "at": x.at} for x in self.log[-500:]]}
        try:
            with self._save_lock:  # ajanlar aynı anda kaydetmeye çalışabilir
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
                   project_id=data.get("id") or f.stem, title=data.get("title", ""),
                   review=bool(data.get("review", True)), lead_model=data.get("lead_model", "brain"),
                   lead_thinking=data.get("lead_thinking", "default"),
                   cwd=Path(data["cwd"]) if data.get("cwd") else None)
        team.created = float(data.get("created") or time.time())
        team.summary = data.get("summary", "")
        team.finished = bool(data.get("finished"))
        team.lead_last = data.get("lead_last", "")
        team.lead_saved = list(data.get("lead_messages") or [])
        team.lead_state = "done" if team.finished else "idle"
        team.questions = list(data.get("questions") or [])
        for d in data.get("members") or []:
            m = Member(name=d["name"], task=d.get("task", ""), constraints=d.get("constraints", ""),
                       scope=list(d.get("scope") or []), model=d.get("model", "brain"),
                       thinking=d.get("thinking", "default"), isolation=d.get("isolation", "shared"),
                       max_steps=int(d.get("max_steps") or 60), after=list(d.get("after") or []),
                       cwd=Path(d.get("cwd") or team.cwd), branch=d.get("branch", ""), base_sha=d.get("base_sha", ""),
                       result=d.get("result", ""), steps=int(d.get("steps") or 0), reviews=int(d.get("reviews") or 0),
                       last=d.get("last", ""), started=float(d.get("started") or 0), ended=float(d.get("ended") or 0),
                       files=list(d.get("files") or []), originals=dict(d.get("originals") or {}),
                       saved_messages=list(d.get("messages") or []))
            # Kapanırken yarıda kalanlar durmuş sayılır; bitmiş, incelemedeki ve hatalılar olduğu gibi kalır.
            m.status = d.get("status") if d.get("status") in ("done", "failed", "review") else "stopped"
            m.ended = m.ended or team.created
            team.members[m.name] = m
            team.inbox[m.name] = []
        team.stopped = not team.finished  # yüklenen yarım proje kullanıcı sürdürene kadar durur
        team.log = [Message(x.get("from", "?"), x.get("to", "?"), x.get("text", ""), float(x.get("at") or 0))
                    for x in data.get("log") or []]
        return team

    def thread_view(self, name: str) -> dict[str, Any]:
        """Bir ajanın (ya da yöneticinin) konuşması ve bilgileri."""
        if name == LEAD:
            msgs = self.lead_agent.messages[1:] if self.lead_agent is not None else self.lead_saved
            return {"name": LEAD, "status": self.lead_state, "model": self.lead_model, "thinking": self.lead_thinking,
                    "task": "Projenin yöneticisi: planlar, soruları cevaplar, biten işleri inceler, sonucu özetler.",
                    "isolation": "shared", "steps": 0, "secs": 0, "files": [], "scope": [], "messages": msgs,
                    "result": self.summary or self.lead_last, "after": [], "constraints": "", "branch": "",
                    "question": "", "reviews": 0, "limit_hit": False, "last": ""}
        m = self.members.get(name)
        if m is None:
            raise ToolError(f"projede '{name}' yok")
        return {**m.snapshot(), "messages": m.messages()}

    member_view = thread_view

    def add_member(self, spec: dict[str, Any], announce: bool = True) -> Member:
        """Çalışan ya da bitmiş projeye yeni ajan ekle ve başlat; diğerlerine haber ver."""
        m = self.add(spec)
        self.stopped = self.finished = False
        self._nudges = 0
        if announce and len(self.members) > 1:
            others = [n for n in self.members if n != m.name]
            note = f"takıma yeni ajan katıldı: {m.name}: {m.task[:200]}" + (f" (yazar: {', '.join(m.scope)})" if m.scope else "")
            with self.cond:
                msg = Message(SYSTEM, "all", note)
                self.log.append(msg)
                for n in others:  # bitmiş ajanları uyandırmadan sadece haber ver
                    self.inbox[n].append(msg)
        self._launch(m)
        self._changed(save=True)
        return m

    def _emit(self, text: str, kind: str = "info") -> None:
        try:
            if self._live_ui:
                self.publish({"type": "project_note", "id": self.id, "title": self.title, "text": text, "kind": kind})
            else:
                self.parent_ctx.emit(f"proje: {text}")
        except Exception:  # noqa: BLE001
            pass

    def stop(self, name: str | None = None) -> None:
        targets = [self.members[name]] if name else list(self.members.values())
        if not name:
            self.stopped = True
            if self.lead_agent is not None:
                self.lead_agent.stop()
        for m in targets:
            if m.status == "queued":
                m.status = "stopped"
            m.resume_wanted = False
            if m.agent is not None and m.status in ACTIVE:
                m.agent.stop()
        with self.cond:
            self.cond.notify_all()
        self._changed(save=True)

    def resume(self) -> None:
        """Durdurulmuş projeyi sürdür: durmuş ajanlar kaldığı yerden devam eder, yönetici uyanır."""
        self.stopped = False
        self._nudges = 0
        for m in self.members.values():
            if m.status == "stopped":
                if m.agent is None and not m.saved_messages:
                    m.status = "queued"
                    self._launch(m)
                else:
                    self._launch(m, resume=True)
        self.event("Kullanıcı projeyi sürdürdü. Durumu gözden geçir, gerekiyorsa ajanlara yön ver.")
        self._changed(save=True)

    def cleanup(self) -> None:
        router = self.parent_ctx.router
        for role in self.roles:
            router.role_overrides.pop(role, None)
            router.thinking_overrides.pop(role, None)
        self.roles.clear()


class MemberUI:
    """Ajanın arayüzü: olayları proje durumuna yansıtır, izin sorularını ajanın adıyla kullanıcıya iletir."""

    def __init__(self, team: Team, member: Member):
        self.team, self.member = team, member

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def info(self, msg: str) -> None:
        if "adım limiti" in msg:
            self.member.limit_hit = True

    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None:
        if result.is_error:
            self.member.last = f"{name} hata: {result.text[:90]}"
            self.team._changed()

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        parent = self.team.parent_ctx.state.get("agent")
        ask = getattr(getattr(parent, "ui", None), "ask_permission", None)
        if ask is None:
            return "n"
        real = self.member.name in self.team.members
        prev = self.member.status
        if real:
            self.member.status = "waiting"
            self.team._changed()
        try:
            with self.team.perm_lock:
                ans = ask(name, f"[{self.team.title[:30]} / {self.member.name}] {summary}", risk)
        finally:
            if real and self.member.status == "waiting":
                self.member.status = prev
                self.team._changed()
        return "y" if ans == "a" else ans  # "her zaman" tek ajana değil, ana oturuma ait kalsın


class TeamSend(Tool):
    name = "team_send"
    description = ("Takım arkadaşına, yöneticiye (alice) ya da herkese mesaj gönderir. Arayüz/sözleşme duyurmak, karar "
                   "sormak, başkasının alanında bir değişiklik istemek için. Kısa ve net yaz.")
    parameters = obj({
        "to": {"type": "string", "description": f"Ajan adı, '{LEAD}' (yönetici) ya da 'all' (herkes)."},
        "text": {"type": "string", "description": "Mesaj."},
    }, ["to", "text"])
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me
        if me == LEAD:
            self.name = "message"
            self.description = ("Ajana ya da herkese ('all') mesaj gönderir: soruya cevap, yön değişikliği, yeni bilgi. "
                                "Bitmiş ajana yazınca kaldığı yerden devam eder.")

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"

    def run(self, ctx: ToolContext, to: str, text: str) -> ToolResult:
        return ToolResult(self.team.send(self.me, str(to).strip().lower(), text))


class TeamWait(Tool):
    name = "team_wait"
    description = ("Sana mesaj gelene kadar bekler (en çok `seconds` saniye) ve gelenleri döndürür. Bir soruya cevap "
                   "beklerken ya da başka bir ajanın işine bağlıyken kullan; boşuna bekleme.")
    parameters = obj({"seconds": {"type": "integer", "description": "En çok kaç saniye. Varsayılan 120, en çok 600."}})
    risk = READ

    def __init__(self, team: Team, me: str):
        self.team, self.me = team, me

    def run(self, ctx: ToolContext, seconds: int = 120) -> ToolResult:
        m = self.team.members[self.me]
        m.status = "asking" if m.question else "waiting"
        self.team._changed()
        try:
            msgs = self.team.wait_messages(self.me, max(1, min(int(seconds or 120), 600)), ctx.cancelled)
        finally:
            if m.status in ("asking", "waiting"):
                m.status = "running"
            self.team._changed()
        if not msgs:
            return ToolResult("süre doldu, mesaj gelmedi. Bekleyebileceğin başka bir şey yoksa işine devam et.")
        return ToolResult("\n".join(x.line() for x in msgs))


class LeadAssign(Tool):
    name = "assign"
    description = ("Projeye yeni ajan ekler ve hemen başlatır (after verilmişse onlar onaylanınca başlar). Görevi, "
                   "konuşmayı görmeyen birinin anlayacağı netlikte yaz.")
    parameters = obj({
        "name": {"type": "string", "description": "Kısa ad, ör. 'backend', 'tests', 'ui'."},
        "task": {"type": "string", "description": "Yapacağı iş: hangi dosyalar, ne değişecek, nasıl doğrulayacak, ne "
                 "raporlayacak."},
        "constraints": {"type": "string", "description": "Sınırlar: dokunmayacağı yerler, uyacağı kurallar."},
        "scope": {"type": "array", "items": {"type": "string"},
                  "description": "Yazabileceği dosya/klasörler (çalışma klasörüne göre). Boş: her yer."},
        "model": {"type": "string", "description": "brain (güçlü), fast (hızlı) ya da sağlayıcı:model."},
        "thinking": {"type": "string", "enum": list(THINKING), "description": "Düşünme seviyesi."},
        "isolation": {"type": "string", "enum": list(ISOLATIONS),
                      "description": "shared: aynı klasör | worktree: kendi git dalı (onayda birleştirilir) | "
                      "readonly: sadece inceleme."},
        "after": {"type": "array", "items": {"type": "string"},
                  "description": "Bu ajanlar onaylanınca başlasın (ör. testler backend'den sonra)."},
        "max_steps": {"type": "integer", "description": "En çok araç adımı. Varsayılan 60."},
    }, ["name", "task"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('name')}: {str(args.get('task', ''))[:110]}"

    def run(self, ctx: ToolContext, **spec: Any) -> ToolResult:
        m = self.team.add_member(spec)
        when = f"; {', '.join(m.after)} onaylanınca başlayacak" if m.status == "queued" and m.after else ""
        return ToolResult(f"{m.name} eklendi ({m.status}{when})")


class LeadReview(Tool):
    name = "review"
    description = ("İnceleme bekleyen ajanın işini onaylar ya da düzeltmeye gönderir. Onaylanan worktree dalı ana "
                   "çalışma klasörüne birleştirilir.")
    parameters = obj({
        "name": {"type": "string", "description": "Ajan adı."},
        "decision": {"type": "string", "enum": ["approve", "revise"], "description": "approve: onayla | revise: "
                     "düzeltme iste."},
        "feedback": {"type": "string", "description": "revise için: neyin yanlış/eksik olduğu ve ne beklediğin."},
    }, ["name", "decision"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('name')}: {args.get('decision')}"

    def run(self, ctx: ToolContext, name: str, decision: str, feedback: str = "") -> ToolResult:
        return ToolResult(self.team.review_member(str(name).strip().lower(), decision == "approve", feedback))


class LeadDiff(Tool):
    name = "diff"
    description = "Ajanın dosya araçlarıyla yaptığı değişikliklerin farkını (unified diff) gösterir."
    parameters = obj({"name": {"type": "string", "description": "Ajan adı."}}, ["name"])
    risk = READ
    parallel_safe = True

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, name: str) -> ToolResult:
        return ToolResult(self.team.diff(str(name).strip().lower(), limit=20_000) or "fark yok")


class LeadAskUser(Tool):
    name = "ask_user"
    description = ("Kullanıcıya soru sorar (Projeler sayfasında görünür). Sadece kullanıcının tercihine bağlı, senin "
                   "karar veremeyeceğin şeyler için. Cevap gelince sana olay olarak gelir; bu arada turunu bitir.")
    parameters = obj({"question": {"type": "string", "description": "Kısa, net soru; gerekiyorsa seçenekleriyle."}},
                     ["question"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, question: str) -> ToolResult:
        if not (question or "").strip():
            raise ToolError("soru boş")
        self.team.ask_user(question)
        return ToolResult("soru kullanıcıya iletildi; cevap gelince sana olay olarak gelecek.")


class LeadFinish(Tool):
    name = "finish"
    description = ("Projeyi bitirir ve kullanıcıya özet yazar. Ancak bütün işler onaylanıp bütün doğrulandıktan sonra "
                   "çağır.")
    parameters = obj({"summary": {"type": "string", "description": "Ne yapıldı, hangi dosyalar, nasıl doğrulandı, "
                                  "açık kalanlar. Markdown olabilir."}}, ["summary"])
    risk = READ

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext, summary: str) -> ToolResult:
        open_ = [m.name for m in self.team.members.values() if m.status in ACTIVE + ("review", "queued")]
        if open_:
            return ToolResult(f"henüz bitmemiş ya da incelenmemiş ajanlar var: {', '.join(open_)}. Önce onları "
                              "bitir/onayla ya da durdur.", is_error=True)
        self.team.finish(summary or "")
        return ToolResult("proje bitti; özet kullanıcıya iletildi.")


class LeadStatus(Tool):
    name = "project_status"
    description = "Projedeki ajanların güncel durumu."
    parameters = obj({})
    risk = READ
    parallel_safe = True

    def __init__(self, team: Team):
        self.team = team

    def run(self, ctx: ToolContext) -> ToolResult:
        return ToolResult(self.team.status_text())
