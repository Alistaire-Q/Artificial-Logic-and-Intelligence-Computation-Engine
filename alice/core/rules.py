"""Kurallar: "şu metrik uzun süre şöyle olursa haber ver".

Karar zinciri ucuzdan pahalıya:
  1. Kural: metrik eşiği belirtilen süre boyunca aşıyor mu? (yapay zekâ yok)
  2. confirm doluysa: durum belirsiz, Jev'e (System One) "haber vermeye değer mi?" sorulur
  3. action = "agent" ise: ALICE'in kendisi uyanır, prompt'taki araştırmayı yapar, sonucu söyler

Kurallar ~/.alice/rules.json'da durur. Kullanıcı kuralı elle yazmaz; "nabzım uzun süre
yüksek kalırsa beni uyar" der, `parse_rule` bunu hızlı modelle kurala çevirir.
"""

from __future__ import annotations

import fnmatch
import operator
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from alice.config import alice_home
from alice.core.notify import Notification, Notifier
from alice.core.store import Store

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}

# Süresiz (duration=0) kurallarda son ölçüm bundan eskiyse kural değerlendirilmez.
FRESH = 120.0


@dataclass
class Rule:
    metric: str                       # anahtar ya da glob: "pc.gpu.temp", "pc.disk.*.percent"
    op: str
    threshold: float
    message: str                      # {value}, {key}, {match}, {threshold} yer tutucuları
    duration: float = 0.0             # saniye; koşul bu kadar süre kesintisiz sağlanmalı
    cooldown: float = 1800.0          # aynı anahtar için tekrar uyarmadan önce beklenecek süre
    severity: str = "warn"            # info | warn | critical
    topic: str = "pc"
    modes: list[str] = field(default_factory=list)  # boşsa her modda; doluysa sadece bu modlarda
    confirm: str = ""                 # doluysa Jev'e sorulacak evet/hayır sorusu
    action: str = "notify"            # notify | agent
    prompt: str = ""                  # action=agent ise ALICE'e verilecek görev
    description: str = ""             # kullanıcının kendi cümlesi
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def __post_init__(self) -> None:
        if self.op not in OPS:
            raise ValueError(f"geçersiz operatör: {self.op}")
        if self.action not in ("notify", "agent"):
            raise ValueError(f"geçersiz aksiyon: {self.action}")
        if self.action == "agent" and not self.prompt:
            raise ValueError("action=agent için prompt gerekli")
        self.threshold = float(self.threshold)
        self.duration = float(self.duration)
        self.cooldown = float(self.cooldown)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary(self) -> str:
        dur = f" {fmt_duration(self.duration)} boyunca" if self.duration else ""
        modes = f" [{', '.join(self.modes)}]" if self.modes else ""
        extra = " +Jev onayı" if self.confirm else ""
        act = " -> ALICE araştırır" if self.action == "agent" else ""
        off = "" if self.enabled else " (kapalı)"
        return f"{self.id}: {self.metric} {self.op} {self.threshold:g}{dur}{modes}{extra}{act} ({self.severity}){off}"


def fmt_duration(sec: float) -> str:
    if sec >= 3600 and sec % 3600 == 0:
        return f"{sec / 3600:g} sa"
    if sec >= 60:
        return f"{sec / 60:g} dk"
    return f"{sec:g} sn"


DEFAULT_RULES = [
    Rule("pc.gpu.temp", ">", 85, "Ekran kartı {value:.0f}°C, birkaç dakikadır çok sıcak.", duration=180,
         description="GPU uzun süre çok ısınırsa söyle", id="gpu-hot"),
    Rule("pc.cpu.temp", ">", 90, "İşlemci {value:.0f}°C, birkaç dakikadır çok sıcak.", duration=180,
         description="CPU uzun süre çok ısınırsa söyle", id="cpu-hot"),
    Rule("pc.mem.percent", ">", 92, "Bellek %{value:.0f} dolu.", duration=300,
         description="RAM uzun süre dolu kalırsa söyle", id="mem-full"),
    Rule("pc.disk.*.percent", ">", 95, "{match} diski %{value:.0f} dolu.", cooldown=6 * 3600,
         description="disk dolmak üzereyse söyle", id="disk-full"),
    Rule("pc.disk.*.free_gb", "<", 10, "{match} diskinde {value:.1f} GB boş yer kaldı. Ne yer kaplıyor bakayım mı?",
         cooldown=6 * 3600, description="bir diskte 10 GB'tan az yer kalırsa söyle", id="disk-low"),
    Rule("pc.proc.*.mem_mb", ">", 8000, "{match} {value:.0f} MB bellek kullanıyor ve düşmüyor.", duration=600,
         cooldown=3 * 3600, description="bir program belleği şişirirse söyle", id="proc-bloat"),
    Rule("watch.hr", ">", 125, "Nabzın {value:.0f}, bir süredir yüksek. İyi misin?", duration=600, cooldown=2 * 3600,
         topic="health", modes=["normal", "dev", "quiet"],
         confirm="The user is likely sitting at the computer (not exercising). Is this sustained high heart rate "
                 "worth a gentle check-in?",
         description="nabzım uzun süre yüksek kalırsa beni uyar", id="hr-high"),
]

# rules.json ilk yazıldıktan sonra eklenen varsayılanlar: yüklerken eksikse eklenir.
ADDED_LATER = {"disk-low"}


class RuleSet:
    def __init__(self, path: Path | None = None):
        self.path = path or alice_home() / "rules.json"
        self.rules: list[Rule] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.rules = [Rule.from_dict(asdict(r)) for r in DEFAULT_RULES]
            return
        from alice.core.schedule import read_json_list

        data = read_json_list(self.path)
        if data is None:  # bozuk dosya: eldekini koru, çekirdek düşmesin
            return
        rules = []
        for d in data:
            try:
                rules.append(Rule.from_dict(d))
            except (TypeError, ValueError, AttributeError):
                continue
        # Sonradan eklenen varsayılan kural eski rules.json'a da girsin; kullanıcı onu silmişse geri gelmesin.
        have = {r.id for r in rules}
        removed = self._removed()
        # Varsayılanlardan hiçbiri yoksa kullanıcı hepsini silmiş: yenisini de dayatma.
        had_defaults = bool(have & {r.id for r in DEFAULT_RULES if r.id not in ADDED_LATER})
        added = [Rule.from_dict(asdict(r)) for r in DEFAULT_RULES
                 if had_defaults and r.id in ADDED_LATER and r.id not in have and r.id not in removed]
        self.rules = rules + added
        if added:
            self.save()

    def _removed_path(self) -> Path:
        return self.path.with_name(self.path.stem + "-removed.json")

    def _removed(self) -> set[str]:
        from alice.core.schedule import read_json_list

        return {str(x) for x in (read_json_list(self._removed_path()) or [])}

    def save(self) -> None:
        from alice.core.schedule import write_json_atomic

        write_json_atomic(self.path, [asdict(r) for r in self.rules])

    def add(self, rule: Rule) -> Rule:
        self.rules = [r for r in self.rules if r.id != rule.id] + [rule]
        self.save()
        return rule

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        if len(self.rules) != before:
            self.save()
            if rule_id in ADDED_LATER:
                from alice.core.schedule import write_json_atomic

                write_json_atomic(self._removed_path(), sorted(self._removed() | {rule_id}))
            return True
        return False

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        r = self.get(rule_id)
        if r is None:
            return False
        r.enabled = enabled
        self.save()
        return True


@dataclass
class Firing:
    rule: Rule
    key: str
    value: float
    text: str


class RuleEngine:
    """Kuralları depodaki veriye karşı değerlendirir ve tetiklenenleri bildirir."""

    def __init__(self, rules: RuleSet, store: Store, notifier: Notifier, decider=None,
                 run_agent: Callable[[str], str] | None = None, mode: Callable[[], str] = lambda: "normal"):
        self.rules = rules
        self.store = store
        self.notifier = notifier
        self.decider = decider
        self.run_agent = run_agent
        self.sync_agent = False  # testler için: ajan eylemini aynı iş parçacığında çalıştır
        self.mode = mode
        self.last_fired: dict[tuple[str, str], float] = {}

    def check(self, rule: Rule, key: str, now: float) -> float | None:
        """Koşul sağlanıyorsa son değeri, sağlanmıyorsa None döndürür."""
        cmp = OPS[rule.op]
        if rule.duration <= 0:
            last = self.store.last(key)
            if not last or last[1] is None or now - last[0] > FRESH:
                return None
            return last[1] if cmp(last[1], rule.threshold) else None
        pts = self.store.series(key, since=now - rule.duration, until=now)
        if not pts or any(not cmp(v, rule.threshold) for _, v in pts):
            return None
        # Ölçümler pencerenin büyük kısmını kapsamıyorsa (servis yeni açıldı, veri kesildi)
        # koşul henüz "uzun süre" sağlanmış sayılmaz; son ölçüm de taze olmalı.
        if pts[0][0] > now - rule.duration * 0.8 or now - pts[-1][0] > FRESH:
            return None
        return pts[-1][1]

    def evaluate(self, now: float | None = None) -> list[Firing]:
        now = time.time() if now is None else now
        mode = self.mode()
        fired: list[Firing] = []
        all_keys: list[str] | None = None
        for rule in self.rules.rules:
            if not rule.enabled or (rule.modes and mode not in rule.modes):
                continue
            if any(c in rule.metric for c in "*?["):
                if all_keys is None:
                    all_keys = self.store.keys(since=now - max(3600.0, FRESH))
                keys = [k for k in all_keys if fnmatch.fnmatchcase(k, rule.metric)]
            else:
                keys = [rule.metric]
            for key in keys:
                if now - self.last_fired.get((rule.id, key), -1e18) < rule.cooldown:
                    continue
                try:
                    value = self.check(rule, key, now)
                    if value is None:
                        continue
                    text = self._format(rule, key, value)
                    if rule.confirm and not self._confirmed(rule, key, value, text):
                        continue
                    self.last_fired[(rule.id, key)] = now
                    fired.append(Firing(rule, key, value, text))
                    self._act(fired[-1])
                except Exception as e:  # noqa: BLE001 - tek bozuk kural çekirdek turunu ve diğer kuralları durdurmasın
                    self.last_fired[(rule.id, key)] = now  # her turda aynı hatayı tekrarlama
                    print(f"kural hatası ({rule.id}, {key}): {e!r}")
        return fired

    @staticmethod
    def _format(rule: Rule, key: str, value: float) -> str:
        match = ""
        if "*" in rule.metric:
            pre, _, post = rule.metric.partition("*")
            match = key[len(pre):len(key) - len(post) if post else None]
        try:
            return rule.message.format(value=value, key=key, match=match, threshold=rule.threshold)
        except (KeyError, IndexError, ValueError):
            return f"{rule.message} ({key} = {value:g})"

    def _confirmed(self, rule: Rule, key: str, value: float, text: str) -> bool:
        if self.decider is None:
            return True
        from alice.decider import DeciderError, Noul

        recent = self.store.series(key, since=time.time() - 3600)[-12:]
        state = (f"Mode: {self.mode()}. Metric {key} = {value:g} (rule: {rule.op} {rule.threshold:g}). "
                 f"Recent values: {', '.join(f'{v:g}' for _, v in recent)}. Alert text: {text}. "
                 f"User's own words: {rule.description}")
        try:
            d = self.decider.decide(state, {"notify": Noul(rule.confirm)})
            return float(d["notify"].value) >= 0.5
        except (DeciderError, Exception):  # noqa: BLE001 - karar motoru yoksa/bozuk cevap verirse kural tek başına karar verir
            return True

    def _act(self, f: Firing) -> None:
        r = f.rule
        if r.action == "agent" and self.run_agent is not None:
            # Ajan turu dakikalar sürebilir: çekirdek turunu (izleme, görevler, durum dosyası) bekletmesin.
            def work() -> None:
                prompt = f"{r.prompt}\n\n(Bu görevi şu uyarı tetikledi: {f.text})"
                try:
                    body = self.run_agent(prompt)
                except Exception as e:  # noqa: BLE001 - ajan çökse de uyarı kaybolmasın
                    body = f"{f.text}\n(araştırma başarısız: {e})"
                try:
                    self.notifier.send(Notification("ALICE", (body or f.text)[:500], r.severity, r.topic))
                except Exception:  # noqa: BLE001
                    pass

            if self.sync_agent:
                work()
            else:
                threading.Thread(target=work, daemon=True, name="alice-rule-agent").start()
        else:
            self.notifier.send(Notification("ALICE", f.text, r.severity, r.topic))


# --------------------------------------------------------- doğal dil -> kural
PARSE_PROMPT = """Convert the user's request into a monitoring rule for a background assistant.
Available metric keys (glob * allowed): {keys}
Common keys: pc.cpu.percent, pc.cpu.temp, pc.gpu.temp, pc.gpu.util, pc.gpu.mem_percent, pc.mem.percent,
pc.disk.<drive>.percent, pc.proc.<name>.mem_mb, pc.proc.<name>.cpu, watch.hr (heart rate), watch.stress,
dcs.fuel.kg, dcs.fuel.min_left, dcs.g, dcs.chaff, dcs.flare.

Reply ONLY with JSON:
{{"metric": "<key or glob>", "op": ">|>=|<|<=|==|!=", "threshold": <number>,
  "duration": <seconds the condition must hold, 0 if instant>, "cooldown": <seconds before repeating, default 1800>,
  "severity": "info|warn|critical", "topic": "pc|flight|health|other",
  "modes": [<subset of "normal","dev","flight","quiet"; empty = always>],
  "message": "<short Turkish alert, may use {{value}}, {{match}}>",
  "confirm": "<optional yes/no question for a fast classifier when the rule alone could false-alarm, else empty>",
  "action": "notify|agent", "prompt": "<if agent: what ALICE should investigate, in Turkish>"}}
User request: {text}"""


def parse_rule(text: str, router, store: Store | None = None) -> Rule:
    from alice.decider import extract_json

    keys = ", ".join(store.keys(since=time.time() - 86400)[:80]) if store else "(henüz veri yok)"
    msgs = [{"role": "user", "content": PARSE_PROMPT.format(keys=keys or "(henüz veri yok)", text=text)}]
    res = router.chat("fast", msgs, temperature=0, max_tokens=800)
    data = extract_json(res.text)
    if not isinstance(data, dict):
        raise ValueError(f"model kural döndürmedi: {res.text[:200]}")
    data["description"] = text
    data.pop("id", None)
    return Rule.from_dict(data)
