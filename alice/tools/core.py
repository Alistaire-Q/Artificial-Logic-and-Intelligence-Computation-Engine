"""Çekirdekle konuşan araçlar: geçmiş metrikler, kurallar, zamanlanmış görevler, mod.

Böylece "dün DCS oynarken GPU kaç dereceydi?", "nabzım uzun süre yüksek kalırsa beni uyar",
"her pazar gece projelerimi yedekle" gibi istekleri beyin modeli doğrudan karşılar.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from alice.tools.base import DANGER, READ, WRITE, Tool, ToolContext, ToolError, ToolResult, obj


def _store(ctx: ToolContext):
    from alice.core.store import Store

    st = ctx.state.get("core_store")
    if st is None:
        st = ctx.state["core_store"] = Store()
    return st


class Metrics(Tool):
    name = "metrics"
    parallel_safe = False  # sqlite bağlantısı ctx.state'te paylaşılıyor
    description = (
        "ALICE çekirdeğinin kaydettiği geçmiş veriler (bilgisayar sıcaklıkları, CPU/GPU/RAM/disk, süreçler; ileride "
        "DCS ve saat). action: status (servis durumu ve mod) | keys (anahtar listesi, pattern glob) | "
        "stats (ort/min/max) | series (zaman serisi, en fazla 60 nokta) | events (bildirimler, susturulanlar, görev "
        "sonuçları) | modes (bir modun açık olduğu aralıklar). since/until: '2h', '3d', 'dün', 'bugün' ya da ISO "
        "tarih. mode: sadece o mod açıkken (flight = DCS/War Thunder, dev, quiet, normal). "
        "Ör. 'dün DCS oynarken GPU kaç derece' -> stats key=pc.gpu.temp since=dün until=bugün mode=flight.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "keys", "stats", "series", "events", "modes"]},
        "key": {"type": "string"}, "pattern": {"type": "string"},
        "since": {"type": "string"}, "until": {"type": "string"},
        "mode": {"type": "string", "enum": ["normal", "dev", "flight", "quiet"]},
    }, ["action"])

    def run(self, ctx: ToolContext, action: str, key: str | None = None, pattern: str = "*",
            since: str | None = None, until: str | None = None, mode: str | None = None) -> ToolResult:
        from alice.core.service import read_state
        from alice.core.store import parse_time

        try:
            t0, t1 = parse_time(since), parse_time(until)
        except ValueError as e:
            raise ToolError(str(e)) from e
        st = _store(ctx)
        if action == "status":
            s = read_state()
            if not s.get("running"):
                return ToolResult("çekirdek çalışmıyor (başlatmak için: alice core run, kalıcı için: alice core install)")
            p = s.get("policy", {})
            return ToolResult(f"çekirdek çalışıyor, mod: {s.get('mode')}{' (elle sabit)' if s.get('forced') else ''}, "
                              f"en düşük bildirim: {p.get('min_severity')}, ses: {p.get('voice')}, "
                              f"yerel GPU: {p.get('local_gpu')}")
        if action == "keys":
            ks = st.keys(pattern or "*", since=t0)
            return ToolResult("\n".join(ks) or "veri yok")
        if action == "events":
            evs = [e for e in st.events(since=t0, limit=100) if t1 is None or e.ts <= t1]
            return ToolResult("\n".join(e.line() for e in evs) or "olay yok")
        if action == "modes":
            out = []
            for m in [mode] if mode else ["flight", "dev", "quiet"]:
                for a, b in st.mode_spans(m, t0, t1):
                    out.append(f"{m}: {datetime.fromtimestamp(a):%d.%m %H:%M} - {datetime.fromtimestamp(b):%H:%M} "
                               f"({(b - a) / 60:.0f} dk)")
            return ToolResult("\n".join(out) or "bu aralıkta mod kaydı yok")
        if not key:
            raise ToolError("key gerekli (anahtarları görmek için action=keys)")
        if action == "stats":
            keys = st.keys(key, since=t0) if any(c in key for c in "*?[") else [key]
            return ToolResult("\n".join(st.stats(k, t0, t1, mode).line() for k in keys[:30]) or "veri yok")
        pts = st.series(key, t0, t1, mode)
        if not pts:
            return ToolResult("veri yok")
        step = max(1, len(pts) // 60)
        rows = [f"{datetime.fromtimestamp(t):%d.%m %H:%M:%S} {v:.1f}" for t, v in pts[::step]]
        return ToolResult(f"{key}, {len(pts)} ölçüm" + (f" (her {step}. gösteriliyor)" if step > 1 else "") + "\n"
                          + "\n".join(rows))


class Rules(Tool):
    name = "rules"
    description = (
        "Çekirdeğin uyarı kuralları. action: list | add | remove | enable | disable. Kullanıcı 'X olursa beni uyar' "
        "dediğinde add ile kural kur: metric (anahtar ya da glob, mevcutlar için metrics keys), op, threshold, "
        "duration (sn, koşulun kesintisiz sürmesi gereken süre), message (Türkçe, {value} {match} kullanılabilir), "
        "severity, topic (pc|flight|health), modes (boş = her mod), confirm (yanlış alarm riski varsa hızlı karar "
        "modeline sorulacak evet/hayır sorusu), action=agent + prompt (tetiklenince ALICE araştırsın). "
        "description'a kullanıcının cümlesini yaz.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable"]},
        "id": {"type": "string"}, "metric": {"type": "string"},
        "op": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="]},
        "threshold": {"type": "number"}, "duration": {"type": "number"}, "cooldown": {"type": "number"},
        "message": {"type": "string"}, "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
        "topic": {"type": "string"}, "modes": {"type": "array", "items": {"type": "string"}},
        "confirm": {"type": "string"}, "rule_action": {"type": "string", "enum": ["notify", "agent"]},
        "prompt": {"type": "string"}, "description": {"type": "string"},
    }, ["action"])
    risk = WRITE

    def risk_for(self, args: dict[str, Any]) -> str:
        if args.get("action") == "list":
            return READ
        # Tetiklenince gözetimsiz ajan çalıştıran kural, onay ister.
        return DANGER if args.get("rule_action") == "agent" else WRITE

    def run(self, ctx: ToolContext, action: str, id: str | None = None, rule_action: str = "notify",
            **fields: Any) -> ToolResult:
        from alice.core.rules import Rule, RuleSet

        rs = RuleSet()
        if action == "list":
            return ToolResult("\n".join(r.summary() for r in rs.rules) or "kural yok")
        if action == "add":
            for req in ("metric", "op", "threshold", "message"):
                if fields.get(req) in (None, ""):
                    raise ToolError(f"add için {req} gerekli")
            try:
                rule = Rule.from_dict({**{k: v for k, v in fields.items() if v is not None}, "action": rule_action,
                                       **({"id": id} if id else {})})
            except (TypeError, ValueError) as e:
                raise ToolError(str(e)) from e
            rs.add(rule)
            return ToolResult(f"kural kuruldu: {rule.summary()}")
        if not id:
            raise ToolError("id gerekli")
        ok = rs.remove(id) if action == "remove" else rs.set_enabled(id, action == "enable")
        return ToolResult("tamam" if ok else f"kural yok: {id}", is_error=not ok)


class Schedule(Tool):
    name = "schedule"
    description = (
        "Zamanlanmış görevler (çekirdek servis çalıştırır). action: list | add | remove | run_now. add: cron "
        "(5 alan: dakika saat gün ay haftanın_günü, 0=pazar), prompt (o saatte ALICE'in gözetimsiz yapacağı işin "
        "eksiksiz Türkçe tarifi, yollar dahil), description (kullanıcının cümlesi), catch_up (bilgisayar kapalıyken "
        "kaçırılırsa açılışta telafi). Kurmadan önce kullanıcıya ne zaman ve ne yapılacağını söyle; onay sistemi "
        "de ayrıca sorar.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "run_now"]},
        "id": {"type": "string"}, "cron": {"type": "string"}, "prompt": {"type": "string"},
        "description": {"type": "string"}, "catch_up": {"type": "boolean"}, "notify": {"type": "boolean"},
    }, ["action"])
    risk = DANGER

    def risk_for(self, args: dict[str, Any]) -> str:
        return READ if args.get("action") == "list" else (WRITE if args.get("action") == "remove" else DANGER)

    def summary(self, args: dict[str, Any]) -> str:
        if args.get("action") == "add" and args.get("cron"):
            from alice.core.schedule import Cron

            try:
                when = Cron(args["cron"]).describe()
            except ValueError:
                when = args["cron"]
            return f"{when}: {args.get('description') or args.get('prompt', '')[:120]}"
        return super().summary(args)

    def run(self, ctx: ToolContext, action: str, id: str | None = None, cron: str | None = None,
            prompt: str | None = None, description: str = "", catch_up: bool = True, notify: bool = True) -> ToolResult:
        from alice.core.schedule import ScheduledTask, TaskStore

        ts = TaskStore()
        if action == "list":
            return ToolResult("\n".join(t.summary() for t in ts.tasks) or "zamanlanmış görev yok")
        if action == "add":
            if not cron or not prompt:
                raise ToolError("add için cron ve prompt gerekli")
            try:
                task = ScheduledTask(cron=cron, prompt=prompt, description=description, catch_up=catch_up,
                                     notify=notify, created=time.time())
            except ValueError as e:
                raise ToolError(str(e)) from e
            ts.add(task)
            from alice.core.service import read_state

            warn = "" if read_state().get("running") else \
                "\nUyarı: çekirdek çalışmıyor; görevin çalışması için `alice core install` ya da `alice core run`."
            return ToolResult(f"görev kuruldu: {task.summary()}{warn}")
        if not id:
            raise ToolError("id gerekli")
        if action == "remove":
            return ToolResult("silindi" if ts.remove(id) else f"görev yok: {id}")
        task = ts.get(id)
        if task is None:
            raise ToolError(f"görev yok: {id}")
        task.pending = True
        ts.save()
        return ToolResult("bir sonraki çekirdek turunda çalışacak")


TOOLS = [Metrics(), Rules(), Schedule()]
