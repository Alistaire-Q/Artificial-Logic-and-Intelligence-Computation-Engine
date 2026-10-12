"""İnceleme kurulu: bir işi, planı ya da fikri farklı bakış açılarıyla inceleyip gerekçeli oylamayla karar veren
ajan heyeti.

Roller: eleştirmen (acımasız ama kanıtlı), savunucu, kontrolcü (tarafsız doğrulayıcı) ve gerekince uzmanlar
(güvenlik, performans, kullanıcı gözü). Akış:

1. Bağımsız inceleme: roller birbirini görmeden aynı anda inceler, bulgu ve oy yazar.
2. Doğrulama: kontrolcü her bulguyu kendisi dener (dosya okur, test çalıştırır) ve doğrulandı/çürütüldü/belirsiz der.
3. Tartışma (derinliğe göre 0-2 tur): herkes diğerlerinin oylarını ve kontrolcünün sonuçlarını görür, bulgulara
   katılır/itiraz eder, oyunu güncelleyebilir. Tartışmada yeni bulgu eklenmez; görüş değişmezse tartışma erken biter.
4. Karar: ağırlıklı oylama. Doğrulanmış kritik bulgu tek başına reddettirir (veto). Bir ret/şartlı oyun dayandığı
   bulguların hepsi çürütüldüyse o oy sayılmaz. Kullanıcı da oy verebilir (3 kat ağırlık) ya da kararı bozabilir.

Döngü yok: tur sayısı en çok 3, her rol bir turda tek ajan turu (adım sınırlı) çalışır.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.tools.base import ToolContext

VOTES = ("approve", "conditional", "reject")
VOTE_TR = {"approve": "onay", "conditional": "şartlı onay", "reject": "ret"}
VOTE_SCORE = {"approve": 1.0, "conditional": 0.75, "reject": 0.0}
APPROVE_AT, REJECT_BELOW = 0.6, 0.4
USER_WEIGHT = 3.0
MAX_ROUNDS = 3
REVIEWER_STEPS = 14
SEVERITIES = ("critical", "major", "minor")
SEV_TR = {"critical": "kritik", "major": "önemli", "minor": "küçük"}
STATUS_TR = {"confirmed": "doğrulandı", "refuted": "çürütüldü", "unclear": "belirsiz", "pending": "bekliyor"}
KIND_TR = {"plan": "Plan kurulu", "work": "İş kurulu", "recheck": "Kontrol turu", "final": "Final kurulu",
           "adhoc": "Kurula sor"}

# Kurulun kullandığı araçlar: okuma ve araştırma. Kontrolcü ayrıca komut çalıştırabilir (testler için).
READ_TOOLS = {"read_file", "list_dir", "glob", "grep", "web_fetch", "web_search", "git"}


@dataclass
class Role:
    key: str
    title: str
    focus: str
    expert: bool = False
    keywords: str = ""


ROLES: dict[str, Role] = {
    "critic": Role("critic", "eleştirmen", (
        "Acımasız ama kanıtlı eleştirmensin. İşteki hataları, eksikleri, gözden kaçmış durumları, riskleri ve kötü "
        "tasarımı bul. Hiçbir şeyi hoş görme, övgü yazma. Ama her bulgu kanıtlı olmalı: dosya:satır, alıntı ya da "
        "tekrar adımı. Kanıtsız bulgu yazma; kontrolcü onu çürütür ve oyun sayılmaz.")),
    "advocate": Role("advocate", "savunucu", (
        "Savunucusun. İşin güçlü yanlarını ve doğru kararlarını ortaya koy; abartılı ya da yanlış eleştirileri kanıtla "
        "çürüt, kurulun işi gereksiz yere reddetmesini engelle. Ama gerçek hataları inkâr etme, dürüst ol.")),
    "verifier": Role("verifier", "kontrolcü", (
        "Tarafsız kontrolcüsün. Taraf tutmazsın: iddiaları kendin kontrol edersin (dosyayı oku, gerekirse testleri "
        "ya da kodu çalıştır) ve her bulgu için doğrulandı/çürütüldü/belirsiz dersin. Dosya değiştirme, kalıcı bir şey "
        "yapma.")),
    "security": Role("security", "güvenlik", (
        "Güvenlik uzmanısın. Güvenlik açıklarına, gizli bilgi (anahtar, şifre) sızıntısına, doğrulanmamış girdiye, "
        "tehlikeli komut/yol kullanımına, yetki hatalarına bak."), True,
        r"auth|token|şifre|password|secret|anahtar|key|izin|permission|güvenlik|security|shell|exec|komut|sql|"
        r"crypto|şifrele|login|giriş|cookie|session|upload|path|yol|http|request|input|girdi|\.env"),
    "performance": Role("performance", "performans", (
        "Performans uzmanısın. Yavaşlığa, gereksiz döngülere ve tekrarlı işlere, bellek kullanımına, ağ/disk "
        "çağrılarına, ölçeklenmeye ve maliyete bak."), True,
        r"performans|performance|hız|yavaş|slow|speed|cache|önbellek|döngü|loop|sorgu|query|bellek|memory|async|"
        r"thread|paralel|büyük|ölçek|scale|optimiz|batch|stream"),
    "ux": Role("ux", "kullanıcı gözü", (
        "Kullanıcı gözüsün. Kullanıcı açısından anlaşılır mı, kullanışlı mı, hata mesajları ve metinler iyi mi, "
        "arayüz tutarlı mı, erişilebilir mi diye bak."), True,
        r"arayüz|ui\b|ux|ekran|buton|düğme|button|css|html|\.js\b|jsx|tsx|sayfa|page|mesaj|metin|text|kullanıcı|"
        r"user|tasarım|design|form|dialog|pencere|menü"),
}
CORE = ("critic", "advocate", "verifier")
# Akıllı model seçiminde rolün alanı
ROLE_DOMAIN = {"critic": "review", "advocate": "review", "verifier": "review", "security": "review",
               "performance": "code", "ux": "design"}


class CouncilStopped(Exception):
    """Proje durduruldu; kurul yarıda bırakıldı."""


@dataclass
class Finding:
    id: str
    by: str
    severity: str
    title: str
    evidence: str = ""
    fix: str = ""
    status: str = "pending"  # pending | confirmed | refuted | unclear
    check: str = ""  # kontrolcünün notu
    notes: list[dict[str, str]] = field(default_factory=list)  # tartışma: {by, stance, note}

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Vote:
    role: str
    model: str
    weight: float
    first: str = ""
    last: str = ""
    reason: str = ""
    conditions: list[str] = field(default_factory=list)
    basis: list[str] = field(default_factory=list)  # dayandığı bulgu kimlikleri
    counted: bool = True
    note: str = ""  # sayılmadıysa nedeni

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def parse_block(text: str) -> dict[str, Any]:
    """Cevabın içindeki son JSON nesnesini bul (```json bloğu ya da düz)."""
    text = text or ""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = blocks[::-1] + [text[i:] for i in [m.start() for m in re.finditer(r"\{", text)][::-1][:6]]
    for c in cands:
        try:
            val, _ = json.JSONDecoder().raw_decode(c.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(val, dict):
            return val
    return {}


_JSON_START = re.compile(r'\{\s*"(?:vote|checks|stances|findings|reason|conditions)"')


def prose(text: str, limit: int = 1500) -> str:
    """Cevabın okunur kısmı: JSON blokları (kararlar zaten oy ve bulgu olarak ayrıca gösteriliyor) atılır."""
    t = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", text or "", flags=re.S)
    m = _JSON_START.search(t)
    if m:
        t = t[:m.start()]
    t = re.sub(r"```(?:json)?\s*$", "", t.strip()).strip()
    return t[:limit].rstrip() + "…" if len(t) > limit else t


def norm_vote(v: Any) -> str:
    v = str(v or "").strip().lower()
    aliases = {"onay": "approve", "onayla": "approve", "approved": "approve", "şartlı": "conditional",
               "sartli": "conditional", "şartlı onay": "conditional", "ret": "reject", "red": "reject",
               "reddet": "reject", "rejected": "reject", "revise": "reject"}
    v = aliases.get(v, v)
    return v if v in VOTES else ""


def relevant_experts(text: str) -> list[str]:
    return [k for k, r in ROLES.items() if r.expert and re.search(r.keywords, text, re.I)]


def plan_council(kind: str, subject: str, size: int, critical: bool, depth: int | str = "auto",
                 roles: list[str] | None = None) -> tuple[list[str], int]:
    """Kurulun üyeleri ve tartışma derinliği (tur sayısı). Alice işe göre seçer: küçük iş hızlı, kritik iş derin."""
    if roles:
        chosen = [r for r in roles if r in ROLES]
    elif critical or kind in ("final", "plan"):
        chosen = list(CORE) + ([k for k, r in ROLES.items() if r.expert] if critical else relevant_experts(subject))
    elif kind == "recheck":
        chosen = ["critic", "verifier"]
    elif size < 80:
        chosen = ["critic", "verifier"] + relevant_experts(subject)[:1]
    else:
        chosen = list(CORE) + relevant_experts(subject)
    if "verifier" not in chosen:
        chosen.append("verifier")
    if isinstance(depth, int) or str(depth).isdigit():
        rounds = max(1, min(int(depth), MAX_ROUNDS))
    elif kind == "recheck":
        rounds = 1
    elif critical or size > 400:
        rounds = 3
    elif size < 80 and kind == "work":
        rounds = 1
    else:
        rounds = 2
    return list(dict.fromkeys(chosen)), rounds


def tally(votes: list[Vote], findings: list[Finding], user: dict[str, Any] | None = None,
          relevant: list[str] | None = None) -> dict[str, Any]:
    """Ağırlıklı oylama. Döner: score, decision (approve|approve_conditions|revise|contested), veto."""
    by_id = {f.id: f for f in findings}
    veto = [f.id for f in findings if f.severity == "critical" and f.status == "confirmed"]
    total = got = 0.0
    for v in votes:
        v.counted, v.note = True, ""
        if not v.last:
            v.counted, v.note = False, "oy vermedi"
            continue
        if v.last in ("reject", "conditional") and v.role != "verifier":
            basis = [by_id[i] for i in v.basis if i in by_id]
            if basis and all(f.status == "refuted" for f in basis):
                v.counted, v.note = False, "dayandığı bulguların hepsi çürütüldü"
                continue
            if not basis and v.last == "reject" and not v.conditions:
                v.counted, v.note = False, "gerekçe gösteren bulgu yok"
                continue
        v.weight = 2.0 if relevant and v.role in relevant else 1.0
        total += v.weight
        got += v.weight * VOTE_SCORE[v.last]
    if user and user.get("vote") in VOTES:
        total += USER_WEIGHT
        got += USER_WEIGHT * VOTE_SCORE[user["vote"]]
    score = got / total if total else 0.0
    conds = dedupe([c for v in votes if v.counted and v.last == "conditional" for c in v.conditions])
    if user and user.get("vote") == "conditional" and user.get("note"):
        conds.append(user["note"])
    if veto:
        decision = "revise"
    elif not total:
        decision = "contested"
    elif score >= APPROVE_AT:
        decision = "approve_conditions" if conds else "approve"
    elif score < REJECT_BELOW:
        decision = "revise"
    else:
        decision = "contested"
    return {"score": round(score, 3), "decision": decision, "veto": veto, "conditions": conds}


class Council:
    def __init__(self, ctx: ToolContext, kind: str, subject: str, context: str, cwd: Path | None = None,
                 critical: bool = False, depth: int | str = "auto", roles: list[str] | None = None,
                 previous: dict[str, Any] | None = None, on_change: Callable[[], None] | None = None,
                 title: str = "", rules: str = "", lessons: list[str] | None = None, check: str = "",
                 images: dict[str, str] | None = None):
        self.ctx = ctx
        self.kind = kind
        self.subject = subject.strip()
        self.context = context
        self.cwd = Path(cwd) if cwd else ctx.cwd
        self.critical = critical
        self.previous = previous  # kontrol turu: önceki kurulun sonucu
        self.rules = rules.strip()  # projenin inceleme kuralları (REVIEW.md)
        self.lessons = lessons or []  # bu projede daha önce yakalanan sorunlar
        self.check = check  # projenin kontrol komutu (kontrolcü bilir)
        self.title = title or self.subject.splitlines()[0][:80]
        self.id = secrets.token_hex(4)
        self.on_usage: Callable[[dict[str, Any]], None] | None = None  # üyelerin token kullanımı (proje sayacı)
        self._stop = threading.Event()  # proje durdurulunca kurul da durur
        self._agents: list[Any] = []
        size = context.count("\n")
        self.roles, self.rounds = plan_council(kind, subject + "\n" + context[:4000], size, critical, depth, roles)
        if previous and kind == "recheck":
            keep = [v["role"] for v in previous.get("votes", []) if v.get("last") != "approve"]
            self.roles = list(dict.fromkeys([r for r in keep if r in ROLES] + ["verifier"]))
        # Sitenin ekran görüntüleri varsa kullanıcı gözü kesin katılır ve görsel kaliteye de bakar.
        self.images = dict(images or {})
        if self.images and "ux" not in self.roles:
            self.roles.insert(max(0, len(self.roles) - 1), "ux")
        self.relevant = [r for r in relevant_experts(subject + "\n" + context[:4000]) if r in self.roles]
        if self.images and "ux" not in self.relevant:
            self.relevant.append("ux")
        self.votes: dict[str, Vote] = {}
        self.findings: list[Finding] = []
        self.transcript: list[dict[str, Any]] = []  # {round, role, text}
        self.state = "running"
        self.round = 0
        self.started = time.time()
        self.ended = 0.0
        self.result: dict[str, Any] = {}
        self.user_vote: dict[str, Any] | None = None
        self.on_change = on_change or (lambda: None)
        self._roles_router: list[str] = []

    # ------------------------------------------------------------- modeller
    def model_for(self, role: str) -> tuple[str, str]:
        """Rolüne ve işin önemine göre model ve düşünme seviyesi."""
        if role == "ux" and self.images:  # ekran görüntülerini görebilen model
            return "vision", "medium" if self.critical or self.kind == "final" else "low"
        if self.critical or self.kind == "final":
            return "brain", "high" if role in ("critic", "verifier") else "medium"
        if role in ("critic", "verifier"):
            return "brain", "medium"
        if role == "ux" and "ux" in self.relevant:  # arayüz işinde tasarım değerlendirmesi zayıf modele kalmasın
            return "brain", "medium"
        return "fast", "low"

    def _agent(self, role: str):
        from alice.agent import Agent, NullUI
        from alice.permissions import Permissions
        from alice.prompts import build_system_prompt

        parent = self.ctx.state.get("agent")
        router = self.ctx.router
        if parent is None or router is None:
            raise RuntimeError("kurul bu bağlamda çalışamıyor")
        allowed = READ_TOOLS | ({"run_command", "process_output", "process_stop"} if role == "verifier" else set())
        tools = [t for n, t in parent.tools.items() if n in allowed]
        cctx = ToolContext(cwd=self.cwd, config=self.ctx.config, router=router, decider=self.ctx.decider,
                           emit=lambda s: None)
        perms = Permissions(parent.permissions.mode if role == "verifier" else "plan")
        perms.always_allow = set(parent.permissions.always_allow)
        ui = parent.ui if role == "verifier" and hasattr(parent.ui, "ask_permission") else NullUI("n")
        agent = Agent(parent.config, router, tools, cctx, perms, ui, parent.decider,
                      system_prompt=build_system_prompt(self.cwd, personality=False) + "\n\n" + self.brief(role),
                      is_child=True)
        agent.stream = False
        agent.ui = _QuietUI(ui)
        model, think = self.model_for(role)
        rname = f"council:{self.id}:{role}"
        if model in ("brain", "vision"):
            # Güçlü model gereken rollerde işin alanına göre en uygun model (eleştiri/kontrol: inceleme,
            # performans: kod, kullanıcı gözü: tasarım ya da ekran görüntüsü varsa görsel).
            from alice.providers import picker

            domain = "vision" if model == "vision" else ROLE_DOMAIN.get(role, "review")
            chain = picker.chain_for(router, domain, fallback=model)
        else:
            chain = list(router.chain(model))
        router.role_overrides[rname] = chain
        router.thinking_overrides[rname] = think
        self._roles_router.append(rname)
        agent.role = rname
        agent.on_usage = self.on_usage
        agent.tool_specs = lambda _a=agent: [t.spec() for t in _a.tools.values()]
        cctx.state["agent"] = agent
        self.votes.setdefault(role, Vote(role, f"{model}·{think}", 1.0))
        return agent

    def brief(self, role: str) -> str:
        r = ROLES[role]
        others = ", ".join(ROLES[x].title for x in self.roles if x != role)
        return f"""# İnceleme kurulu
Kurul rolün: {r.title}. {r.focus}
Kuruldaki diğer roller: {others}. Karar gerekçeli ağırlıklı oylamayla verilir; doğrulanmış kritik bulgu reddettirir.
İnceleme türü: {KIND_TR.get(self.kind, self.kind)}. Çalışma klasörü: {self.cwd}

Kısa ve somut yaz. Araçlarla kodu ve dosyaları kendin incele; tahmin yürütme. Dosya değiştirme.
Önce büyük resme bak: iş hedefi karşılıyor mu, kullanıcı sonuçtan memnun kalır mı? Önem derecesini dürüst ver:
- critical: iş bozuk ya da güvensiz (çalışmıyor, veri kaybı, güvenlik açığı).
- major: hedefi ciddi biçimde karşılamıyor (eksik sayfa/özellik, kırık akış, şablon gibi duran tasarım).
- minor: iyileştirme ve ayrıntı (kod tarzı, küçük kenar durumları, mikro optimizasyon). Küçük ayrıntı işi durdurmaz.
Başlıkları, gerekçeleri ve şartları Türkçe yaz. Aynı şeyi başka sözcüklerle tekrar etme.
Son mesajının sonuna mutlaka istenen JSON bloğunu ```json ... ``` içinde koy.""" + (
            f"\n\n# Projenin inceleme kuralları (REVIEW.md)\n{self.rules}" if self.rules else "") + (
            "\n\n# Bu projede daha önce yakalanan sorunlar (tekrarlanmış mı bak)\n" +
            "\n".join(f"- {x}" for x in self.lessons) if self.lessons else "") + (
            f"\n\nProjenin kontrol komutu: `{self.check}` (çıktısı incelenen malzemede; gerekirse yeniden çalıştır)."
            if self.check and role == "verifier" else "")

    # --------------------------------------------------------------- akış
    def stop(self) -> None:
        """Kurulu durdur: konuşan üyeler kesilir, sonuç "durduruldu" olur."""
        self._stop.set()
        for a in list(self._agents):
            a.stop()

    def run(self) -> dict[str, Any]:
        try:
            self._round1()
            self._verify()
            for _ in range(self.rounds - 1):
                if not self._contested():
                    break
                if not self._debate():
                    break
            self._finish()
        except CouncilStopped:
            self.state = "stopped"
            self.result = {"decision": "stopped", "score": 0.0, "veto": [], "conditions": []}
            self.ended = time.time()
        except Exception as e:  # noqa: BLE001 - kurul çökerse karar "çekişmeli" olsun, iş durmasın
            self.state = "failed"
            self.result = {"decision": "contested", "score": 0.0, "veto": [], "conditions": [],
                           "error": f"{type(e).__name__}: {e}"}
            self.ended = time.time()
        finally:
            self._cleanup()
        self.on_change()
        return self.snapshot()

    def _ask(self, role: str, prompt: str) -> str:
        if self._stop.is_set():
            raise CouncilStopped()
        agent = self._agent(role)
        self._agents.append(agent)
        images = list(self.images.values()) if role == "ux" else None
        if images:
            prompt += ("\n\nEkte sitenin ekran görüntüleri var (" + ", ".join(self.images) + "). Koda ek olarak "
                       "görünüşü de değerlendir: görsel hiyerarşi, boşluklar, tipografi, renk, tutarlılık, telefonda "
                       "taşma/kırılma, şablon gibi ya da 'yapay zekâ yapımı' duran yerler. Görselden gördüğün sorunu "
                       "kanıt olarak hangi görüntüde nerede olduğunu yazarak ver.")
        text = agent.run_turn(prompt, max_steps=REVIEWER_STEPS, images=images) or ""
        if not parse_block(text) and text.strip():
            # Zayıf modeller JSON'u unutabiliyor: bir kez, sadece biçimi iste.
            again = agent.run_turn("Cevabının sonunda istenen JSON bloğu yoktu. Kararını değiştirmeden şimdi sadece o "
                                   "JSON bloğunu ```json ... ``` içinde ver.", max_steps=1) or ""
            if parse_block(again):
                text = text.rstrip() + "\n\n" + again
        self.transcript.append({"round": self.round, "role": role, "text": text})
        if self._stop.is_set():
            raise CouncilStopped()
        return text

    def _parallel(self, roles: list[str], make_prompt: Callable[[str], str]) -> dict[str, str]:
        out: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(roles))) as pool:
            futs = {r: pool.submit(self._ask, r, make_prompt(r)) for r in roles}
            for r, f in futs.items():
                try:
                    out[r] = f.result()
                except CouncilStopped:
                    out[r] = ""
                except Exception as e:  # noqa: BLE001
                    out[r] = ""
                    self.transcript.append({"round": self.round, "role": r, "text": f"(hata: {e})"})
        if self._stop.is_set():
            raise CouncilStopped()
        return out

    def _material(self) -> str:
        prev = ""
        if self.previous:
            confirmed = [f for f in self.previous.get("findings", []) if f.get("status") != "refuted"]
            prev = "\n\n# Önceki kurulun bulguları (düzeltildi mi kontrol et)\n" + "\n".join(
                f"- [{f['id']}] ({SEV_TR.get(f['severity'], f['severity'])}) {f['title']}: {f.get('fix', '')}"
                for f in confirmed) + ("\nŞartlar: " + "; ".join(self.previous.get("conditions", []))
                                       if self.previous.get("conditions") else "")
        return f"# İncelenen\n{self.subject}\n\n{self.context[:24000]}{prev}"

    def _round1(self) -> None:
        self.round = 1
        reviewers = [r for r in self.roles if r != "verifier"] or ["critic"]
        ask = ("Önceki bulgular düzeltildi mi kontrol et; sadece hâlâ duran ya da yeni çıkan ciddi sorunları yaz."
               if self.kind == "recheck" else "Bağımsız incele (diğer rollerin ne dediğini görmüyorsun).")
        fmt = ('{"vote": "approve|conditional|reject", "reason": "kısa gerekçe", "findings": [{"severity": '
               '"critical|major|minor", "title": "...", "evidence": "dosya:satır, alıntı ya da tekrar adımı", "fix": '
               '"ne yapılmalı"}], "conditions": ["şartlı onaysa: şunlar düzelirse olur"]}')
        texts = self._parallel(reviewers, lambda r: f"{self._material()}\n\n{ask}\nSonunda şu biçimde JSON ver:\n{fmt}")
        n = len(self.findings)
        for role, text in texts.items():
            data = parse_block(text)
            v = self.votes.setdefault(role, Vote(role, "?", 1.0))
            v.first = v.last = norm_vote(data.get("vote"))
            v.reason = str(data.get("reason") or "")[:600] or (text.strip()[-300:] if not data else "")
            v.conditions = [str(c)[:300] for c in data.get("conditions") or [] if str(c).strip()][:8]
            for f in (data.get("findings") or [])[:12]:
                if not isinstance(f, dict) or not str(f.get("title") or "").strip():
                    continue
                n += 1
                sev = str(f.get("severity") or "minor").lower()
                fd = Finding(f"F{n}", role, sev if sev in SEVERITIES else "minor", str(f["title"])[:200],
                             str(f.get("evidence") or "")[:800], str(f.get("fix") or "")[:400])
                self.findings.append(fd)
                v.basis.append(fd.id)
        self.on_change()

    def _findings_text(self, with_status: bool = True) -> str:
        if not self.findings:
            return "(bulgu yok)"
        rows = []
        for f in self.findings:
            st = f" — kontrolcü: {STATUS_TR[f.status]}{' (' + f.check + ')' if f.check else ''}" if with_status else ""
            rows.append(f"[{f.id}] {ROLES[f.by].title}, {SEV_TR[f.severity]}: {f.title}\n  kanıt: {f.evidence}"
                        f"\n  öneri: {f.fix}{st}")
        return "\n".join(rows)

    def _verify(self) -> None:
        self.round = max(self.round, 1)
        fmt = ('{"checks": [{"id": "F1", "status": "confirmed|refuted|unclear", "note": "ne denedin, ne gördün"}], '
               '"findings": [{"severity": "...", "title": "...", "evidence": "...", "fix": "..."}], '
               '"vote": "approve|conditional|reject", "reason": "..."}')
        text = self._ask("verifier", f"{self._material()}\n\n# Diğer rollerin bulguları\n"
                         f"{self._findings_text(with_status=False)}\n\nHer bulguyu kendin doğrula (dosyayı oku, gerekirse "
                         f"testleri/kodu çalıştır). Kendi bulduğun kesin sorunları (ör. kırılan test) findings'e ekle. "
                         f"Sonunda şu biçimde JSON ver:\n{fmt}")
        data = parse_block(text)
        self._apply_checks(data)
        v = self.votes.setdefault("verifier", Vote("verifier", "?", 1.0))
        v.first = v.last = norm_vote(data.get("vote"))
        v.reason = str(data.get("reason") or "")[:600]
        n = len(self.findings)
        for f in (data.get("findings") or [])[:6]:
            if isinstance(f, dict) and str(f.get("title") or "").strip():
                n += 1
                sev = str(f.get("severity") or "major").lower()
                self.findings.append(Finding(f"F{n}", "verifier", sev if sev in SEVERITIES else "major",
                                             str(f["title"])[:200], str(f.get("evidence") or "")[:800],
                                             str(f.get("fix") or "")[:400], status="confirmed", check="kendi denedi"))
                v.basis.append(f"F{n}")
        for f in self.findings:  # kontrolcünün bakmadığı bulgular belirsiz kalır
            if f.status == "pending":
                f.status = "unclear"
        self.on_change()

    def _apply_checks(self, data: dict[str, Any]) -> None:
        by_id = {f.id: f for f in self.findings}
        for c in data.get("checks") or []:
            if not isinstance(c, dict):
                continue
            f = by_id.get(str(c.get("id") or "").strip().upper())
            st = str(c.get("status") or "").lower()
            if f is not None and st in ("confirmed", "refuted", "unclear"):
                f.status, f.check = st, str(c.get("note") or "")[:400]

    def _contested(self) -> bool:
        """Tartışmaya değer mi: oylar bölünmüş ya da tartışmalı (belirsiz) bulgu var."""
        r = tally(list(self.votes.values()), self.findings, None, self.relevant)
        kinds = {v.last for v in self.votes.values() if v.last}
        return (r["decision"] == "contested" or len(kinds) > 1 or any(f.status == "unclear" for f in self.findings)) \
            and bool(self.findings)

    def _debate(self) -> bool:
        """Bir tartışma turu. Görüş değişmediyse False (tartışma biter)."""
        self.round += 1
        positions = "\n".join(f"- {ROLES[v.role].title}: {VOTE_TR.get(v.last, 'oy yok')}. {v.reason}"
                              for v in self.votes.values())
        fmt = ('{"vote": "approve|conditional|reject", "reason": "...", "conditions": ["..."], "stances": [{"id": '
               '"F1", "stance": "agree|disagree", "note": "kanıtınla"}], "checks": [{"id": "F1", "status": '
               '"confirmed|refuted|unclear", "note": "sadece kontrolcü"}]}')
        before = {r: v.last for r, v in self.votes.items()}
        texts = self._parallel(self.roles, lambda r: (
            f"{self._material()}\n\n# Bulgular ve kontrolcünün sonuçları\n{self._findings_text()}\n\n# Oylar\n{positions}"
            f"\n\nTartışma turu {self.round}. Yeni bulgu ekleme. Katılmadığın bulgulara kanıtla itiraz et, ikna olduysan "
            f"oyunu değiştir. " + ("Tartışmalı bulguları yeniden doğrula. " if r == "verifier" else "") +
            f"Sonunda şu biçimde JSON ver:\n{fmt}"))
        by_id = {f.id: f for f in self.findings}
        for role, text in texts.items():
            data = parse_block(text)
            v = self.votes.setdefault(role, Vote(role, "?", 1.0))
            nv = norm_vote(data.get("vote"))
            if nv:
                v.last = nv
            if data.get("reason"):
                v.reason = str(data["reason"])[:600]
            if data.get("conditions"):
                v.conditions = [str(c)[:300] for c in data["conditions"] if str(c).strip()][:8]
            for s in data.get("stances") or []:
                f = by_id.get(str((s or {}).get("id") or "").strip().upper()) if isinstance(s, dict) else None
                if f is not None and str(s.get("note") or "").strip():
                    note = {"by": role, "stance": str(s.get("stance") or ""), "note": str(s["note"])[:400]}
                    if note not in f.notes:  # aynı görüşü her turda tekrar yazmasın
                        f.notes.append(note)
                    if str(s.get("stance")) == "agree" and f.id not in v.basis and role != "verifier":
                        v.basis.append(f.id)
            if role == "verifier":
                self._apply_checks(data)
        self.on_change()
        return any(before.get(r) != v.last for r, v in self.votes.items())

    def _finish(self) -> None:
        self.result = tally(list(self.votes.values()), self.findings, self.user_vote, self.relevant)
        self.state = "done"
        self.ended = time.time()

    def _cleanup(self) -> None:
        router = self.ctx.router
        for r in self._roles_router:
            router.role_overrides.pop(r, None)
            router.thinking_overrides.pop(r, None)
        self._roles_router.clear()

    # --------------------------------------------------------------- sonuç
    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state, "round": self.round,
                "rounds": self.rounds, "roles": self.roles, "critical": self.critical, "relevant": self.relevant,
                "votes": [v.as_dict() for v in self.votes.values()], "findings": [f.as_dict() for f in self.findings],
                "result": self.result, "user_vote": self.user_vote, "started": self.started, "ended": self.ended,
                "screens": list(self.images),
                "transcript": [{**t, "text": prose(t["text"])} for t in self.transcript]}


def retally(snap: dict[str, Any], user_vote: dict[str, Any] | None) -> dict[str, Any]:
    """Kaydedilmiş kurul sonucunu kullanıcının oyuyla yeniden say."""
    votes = [Vote(**{k: v for k, v in d.items() if k in Vote.__dataclass_fields__}) for d in snap.get("votes", [])]
    findings = [Finding(**{k: v for k, v in d.items() if k in Finding.__dataclass_fields__})
                for d in snap.get("findings", [])]
    res = tally(votes, findings, user_vote, snap.get("relevant") or [])
    return {**snap, "result": res, "user_vote": user_vote, "votes": [v.as_dict() for v in votes]}


def report(snap: dict[str, Any]) -> str:
    """Kurul sonucunun düz metin özeti (yönetici, ajan ve sohbet için)."""
    res = snap.get("result") or {}
    dec = {"approve": "ONAY", "approve_conditions": "ŞARTLI ONAY", "revise": "DÜZELTME GEREKLİ",
           "contested": "ÇEKİŞMELİ (karar senin)"}.get(res.get("decision", ""), "?")
    lines = [f"{KIND_TR.get(snap.get('kind'), 'Kurul')}: {dec} (onay oranı %{round(100 * res.get('score', 0))}"
             f"{', veto: ' + ', '.join(res['veto']) if res.get('veto') else ''})"]
    if res.get("error"):
        lines.append(f"Kurul tamamlanamadı: {res['error']}")
    for v in snap.get("votes", []):
        role = ROLES.get(v["role"]).title if v["role"] in ROLES else v["role"]
        change = f"{VOTE_TR.get(v['first'], '-')} → " if v.get("first") and v["first"] != v.get("last") else ""
        lines.append(f"- {role}: {change}{VOTE_TR.get(v.get('last'), 'oy yok')}"
                     f"{' (sayılmadı: ' + v['note'] + ')' if not v.get('counted', True) else ''}. {v.get('reason', '')}")
    real = [f for f in snap.get("findings", []) if f.get("status") != "refuted"]
    if real:
        lines.append("Bulgular:")
        for f in real:
            lines.append(f"- [{f['id']}] {SEV_TR.get(f['severity'], f['severity'])} ({STATUS_TR.get(f['status'])}): "
                         f"{f['title']}. Öneri: {f.get('fix', '')}")
    gone = len(snap.get("findings", [])) - len(real)
    if gone:
        lines.append(f"({gone} bulgu kontrolcü tarafından çürütüldü)")
    if res.get("conditions"):
        lines.append("Şartlar: " + "; ".join(res["conditions"]))
    if snap.get("user_vote"):
        lines.append(f"Kullanıcının oyu: {VOTE_TR.get(snap['user_vote'].get('vote'), '?')} "
                     f"{snap['user_vote'].get('note', '')}")
    return "\n".join(lines)


def fix_list(snap: dict[str, Any]) -> str:
    """Ajana gidecek düzeltme listesi: doğrulanmış/belirsiz bulgular ve (bulgularla aynı olmayan) şartlar."""
    res = snap.get("result") or {}
    found = [f for f in snap.get("findings", []) if f.get("status") in ("confirmed", "unclear")]
    items = [f"- [{f['id']}] ({SEV_TR.get(f['severity'], f['severity'])}) {f['title']} — {f.get('fix', '')}"
             f"{' Kanıt: ' + f['evidence'][:200] if f.get('evidence') else ''}" for f in found]
    seen = [f"{f['title']} {f.get('fix', '')}" for f in found]
    items += [f"- şart: {c}" for c in dedupe(res.get("conditions", []), seen)]
    return "\n".join(items) or "- (kurul somut bulgu yazmadı; raporunu gözden geçir)"


def blocking(snap: dict[str, Any]) -> bool:
    """Kurulun istediği düzeltme işi durdurmaya değer mi: doğrulanmış ya da belirsiz kritik/önemli bulgu var mı."""
    return any(f.get("status") in ("confirmed", "unclear") and f.get("severity") in ("critical", "major")
               for f in snap.get("findings", []))


_WORD = re.compile(r"[\wçğıöşüÇĞİÖŞÜ]+", re.U)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def dedupe(items: list[str], against: list[str] | None = None, same: float = 0.5) -> list[str]:
    """Aynı şeyi başka sözcüklerle söyleyen maddeleri ayıkla (sözcük örtüşmesi); `against` ile örtüşenler de atılır."""
    kept: list[str] = []
    pool = [_words(x) for x in (against or [])]
    for it in items:
        w = _words(it)
        if not w:
            continue
        if any(len(w & p) >= 2 and len(w & p) / max(1, min(len(w), len(p))) >= same for p in pool):
            continue
        kept.append(it)
        pool.append(w)
    return kept


class _QuietUI:
    """Kurul üyelerinin arayüzü: izin soruları (sadece kontrolcü) ana arayüze gider, gerisi sessiz."""

    def __init__(self, base: Any):
        self.base = base

    def __getattr__(self, name: str):
        return lambda *a, **k: None

    def ask_permission(self, name: str, summary: str, risk: str) -> str:
        ask = getattr(self.base, "ask_permission", None)
        return ask(name, f"[kurul / kontrolcü] {summary}", risk) if ask else "n"
