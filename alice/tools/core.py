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
    description = ("Çekirdeğin kaydettiği GEÇMİŞ ölçümler ve olaylar: bilgisayar (pc.*), uçuş (dcs.*), saat/nabız "
                   "(watch.hr) gibi. 'Dün DCS'te GPU kaç dereceydi', 'son 2 saatte CPU' gibi sorular için. Şu anki "
                   "durum için system_info. Anahtarı bilmiyorsan önce action=keys.\n"
                   "Ör. dün uçuşta GPU sıcaklığı: action=stats key=pc.gpu.temp since=dün until=bugün mode=flight.")
    parameters = obj({
        "action": {"type": "string", "enum": ["status", "keys", "stats", "series", "events", "modes"],
                   "description": "status: çekirdek çalışıyor mu, hangi mod | keys: pattern'e uyan anahtarlar | stats: "
                   "key için en düşük/ortalama/en yüksek | series: key'in zaman içindeki değerleri | events: olay "
                   "günlüğü | modes: modların açık olduğu zaman aralıkları."},
        "key": {"type": "string", "description": "stats/series için anahtar, ör. 'pc.gpu.temp'; stats'ta glob olabilir "
                "('pc.disk.*.percent')."},
        "pattern": {"type": "string", "description": "keys için glob, ör. 'pc.*'. Varsayılan '*'."},
        "since": {"type": "string", "description": "Başlangıç: '15m', '2h', '3d' (şu andan geriye), 'dün', 'bugün' "
                  "ya da ISO tarih."},
        "until": {"type": "string", "description": "Bitiş, since ile aynı biçim. Boş: şimdi."},
        "mode": {"type": "string", "enum": ["normal", "dev", "flight", "quiet"],
                 "description": "Sadece bu mod açıkken alınan ölçümler (flight = DCS/War Thunder)."},
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
    description = ("Ölçüme bağlı uyarı kuralları: 'GPU 85 dereceyi geçerse', 'nabzım 10 dakika 120'nin üstünde "
                   "kalırsa beni uyar'. Çekirdek ölçümleri izler, koşul sağlanınca bildirir. Belirli bir SAATTE "
                   "hatırlatma ya da iş için schedule kullan. Metrik anahtarını bilmiyorsan önce metrics action=keys.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "enable", "disable"],
                   "description": "list | add: yeni kural (metric, op, threshold, message gerekli) | "
                   "remove/enable/disable: id'li kural."},
        "id": {"type": "string", "description": "remove/enable/disable için kural numarası (list çıktısından)."},
        "metric": {"type": "string", "description": "İzlenecek anahtar ya da glob, ör. 'pc.gpu.temp', "
                   "'pc.disk.*.percent', 'watch.hr'."},
        "op": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="], "description": "Karşılaştırma."},
        "threshold": {"type": "number", "description": "Eşik değer, ör. 85."},
        "duration": {"type": "number", "description": "Koşulun kesintisiz sürmesi gereken saniye. Varsayılan 0."},
        "cooldown": {"type": "number", "description": "Tekrar uyarmadan önce beklenecek saniye. Varsayılan 1800."},
        "message": {"type": "string", "description": "Bildirim metni; {value}, {key}, {match}, {threshold} yer "
                    "tutucuları kullanılabilir, ör. 'GPU {value} derece!'."},
        "severity": {"type": "string", "enum": ["info", "warn", "critical"], "description": "Önem. Varsayılan warn."},
        "topic": {"type": "string", "description": "Konu etiketi, ör. 'pc', 'flight', 'health'. Varsayılan pc."},
        "modes": {"type": "array", "items": {"type": "string"},
                  "description": "Sadece bu modlarda çalışsın, ör. [\"flight\"]. Boş: her modda."},
        "confirm": {"type": "string", "description": "Yanlış alarm riski varsa uyarmadan önce hızlı modele sorulacak "
                    "evet/hayır sorusu."},
        "rule_action": {"type": "string", "enum": ["notify", "agent"],
                        "description": "notify: sadece bildir (varsayılan) | agent: ALICE prompt'taki işi yapsın."},
        "prompt": {"type": "string", "description": "rule_action=agent için ALICE'in yapacağı işin tarifi."},
        "description": {"type": "string", "description": "Kullanıcının kendi cümlesi, ör. 'GPU ısınırsa söyle'."},
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
    description = ("Belirli saatlerde ya da tekrarlı çalışan görevler: 'her sabah 8'de hava durumunu söyle', 'her "
                   "pazar gece projelerimi yedekle', 'yarın 17:00'de bana toplantıyı hatırlat'. O saatte ALICE "
                   "prompt'taki işi gözetimsiz yapar. Kurmadan önce ne zaman ve ne yapılacağını kullanıcıya söyle. "
                   "Ölçüme bağlı uyarılar (sıcaklık, nabız) için rules kullan.")
    parameters = obj({
        "action": {"type": "string", "enum": ["list", "add", "remove", "run_now"],
                   "description": "list | add: yeni görev (cron ve prompt gerekli) | remove: id'li görevi sil | "
                   "run_now: id'li görevi hemen çalıştır."},
        "id": {"type": "string", "description": "remove/run_now için görev numarası (list çıktısından)."},
        "cron": {"type": "string", "description": "5 alanlı cron: dakika saat gün ay haftanın-günü (0 = pazar). "
                 "Ör. '0 8 * * *' her gün 08:00, '30 22 * * 0' pazar 22:30, '0 17 11 10 *' 11 Ekim 17:00."},
        "prompt": {"type": "string", "description": "O saatte yapılacak işin eksiksiz tarifi; konuşmayı görmeyen "
                   "birinin anlayacağı gibi yaz."},
        "description": {"type": "string", "description": "Kısa ad, ör. 'sabah hava durumu'."},
        "catch_up": {"type": "boolean", "description": "Bilgisayar kapalıyken kaçırılırsa açılışta yap. Varsayılan true."},
        "notify": {"type": "boolean", "description": "Bitince sonucu bildir. Varsayılan true."},
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
