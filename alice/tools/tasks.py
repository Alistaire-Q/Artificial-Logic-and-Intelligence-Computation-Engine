"""Çok adımlı görev araçları: run_plan (planla, yürüt, doğrula) ve task_control (onay, durdurma, durum)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class RunPlan(Tool):
    name = "run_plan"
    description = (
        "Birkaç ayrı adımdan oluşan bir isteği planlayıp baştan sona yapar: 2-8 adıma böler, her adımı araçlarla "
        "yapar ve sonucunu kontrol eder; olmayan adımı bir kez başka yoldan dener. Geri alınamaz adımlardan "
        "(mesaj/e-posta göndermek, silmek, satın almak, kurmak) önce kullanıcıya onay sorar (cevap task_control "
        "ile verilir). Dakikalar sürebilir.\n"
        "- Kullan: 'Gmail'deki son faturayı indir, masaüstüne kaydet ve Ahmet'e WhatsApp'tan gönder'.\n"
        "- Kullanma: tek araçla bitecek işler (doğrudan o aracı çağır); tek bir uygulamada ekran işi "
        "(computer_task). Aynı anda tek görev çalışır."
    )
    parameters = obj({"goal": {"type": "string", "description": "Kullanıcının isteği eksiksiz, kişi, dosya ve "
                               "uygulama adlarıyla; konuşmayı görmeyen birinin anlayacağı gibi."}}, ["goal"])
    risk = COMPUTER  # içindeki her araç çağrısı ayrıca izin kontrolünden geçer

    def summary(self, args: dict[str, Any]) -> str:
        return str(args.get("goal", ""))[:150]

    def run(self, ctx: ToolContext, goal: str) -> ToolResult:
        from alice.multistep import TaskRunner, task_log

        if not (goal or "").strip():
            raise ToolError("goal boş")
        if ctx.state.get("active_task") is not None:
            return ToolResult("zaten bir görev sürüyor; bitmesini bekle ya da task_control action=stop ile durdur.",
                              is_error=True)
        a = ctx.config.agent
        hooks = ctx.state.get("task_hooks") or {}
        log = hooks.get("log") or (lambda s: None)

        def both(s: str) -> None:
            task_log(s)
            log(f"görev: {s}")

        runner = TaskRunner(ctx, goal.strip(), max_steps=int(a.get("plan_max_steps", 8)),
                            step_tool_calls=int(a.get("plan_step_tool_calls", 14)),
                            answer_timeout=float(a.get("plan_answer_timeout", 120)), log=both)
        ctx.state["active_task"] = runner
        try:
            text = runner.run()
        finally:
            ctx.state.pop("active_task", None)
        both(f"bitti: {text.splitlines()[0] if text else ''}")
        return ToolResult(text, is_error=text.startswith("error:"))


class TaskControl(Tool):
    name = "task_control"
    description = (
        "Süren run_plan görevini yönetir. Onayı kullanıcı açıkça söylemeden ASLA verme."
    )
    parameters = obj({"action": {"type": "string", "enum": ["approve", "reject", "stop", "status"],
                                 "description": "approve: görev onay sorunca kullanıcı 'evet' dedi | reject: 'hayır' "
                                 "dedi | stop: 'dur', 'vazgeç' | status: 'ne durumda'."}}, ["action"])
    risk = READ

    def run(self, ctx: ToolContext, action: str) -> ToolResult:
        runner = ctx.state.get("active_task")
        if runner is None:
            return ToolResult("şu an süren bir görev yok")
        if action == "status":
            return ToolResult(runner.status_text())
        if action == "stop":
            runner.stop()
            return ToolResult("görev durduruluyor; o anki adım biter bitmez duracak")
        if action in ("approve", "reject"):
            if not runner.answer(action == "approve"):
                return ToolResult("görev şu an onay beklemiyor", is_error=True)
            return ToolResult("onaylandı, devam ediyor" if action == "approve" else "reddedildi, o adım yapılmayacak")
        raise ToolError(f"bilinmeyen action: {action}")


class TeamTool(Tool):
    name = "team"
    description = (
        "Büyük bir işi paralel çalışan bir ajan takımına böler; sen orkestratör olursun. Her üyeye görev, sınırlar, "
        "yazma kapsamı, model, düşünme seviyesi ve çalışma yeri verirsin; üyeler aynı anda çalışır, birbirine ve sana "
        "mesaj atabilir.\n"
        "- Kullan: birbirinden ayrılabilen parçaları olan büyük işler (büyük kod tabanında birden çok modül, "
        "frontend+backend+test, geniş araştırma). Küçük işlerde kullanma: kendin yap ya da task aracını kullan.\n"
        "- Akış: önce kodu kendin kısaca incele ve işi çakışmayan parçalara böl (her üyenin scope'u ayrı olsun). "
        "action=start ile başlat, sonra action=wait ile bekle; gelen sorulara action=send ile cevap ver, bitenlerin "
        "raporlarını oku, gerekirse yeni iş ver (bitmiş üyeye send onu devam ettirir). Hepsi bitince sonucu "
        "birleştir, kendin doğrula (testleri çalıştır) ve kullanıcıya özetle.\n"
        "- Üyeler kod işi araçlarını kullanır (dosya, kabuk, web, git); ekran ve tarayıcı araçları onlara kapalı."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["start", "wait", "send", "status", "stop"],
                   "description": "start: takımı kur ve başlat | wait: sana mesaj gelene ya da bir üye bitene kadar "
                   "bekle | send: üyeye mesaj/yeni iş | status: durum | stop: üyeyi (name) ya da herkesi durdur."},
        "goal": {"type": "string", "description": "start: işin genel hedefi ve bağlamı; üyelerin hepsi görür. "
                 "Kullanıcının isteğini, kod tabanı hakkında bildiklerini ve bitti sayılma ölçütünü yaz."},
        "agents": {"type": "array", "description": "start: üyeler (en çok 8).", "items": obj({
            "name": {"type": "string", "description": "Kısa ad, ör. 'backend', 'tests', 'ui'."},
            "task": {"type": "string", "description": "Yapacağı iş, konuşmayı görmeyen biri anlayacak kadar ayrıntılı: "
                     "hangi dosyalar, ne değişecek, nasıl doğrulayacak, ne raporlayacak."},
            "constraints": {"type": "string", "description": "Sınırlar: dokunmayacağı yerler, uyacağı kurallar, "
                            "kullanmayacağı yöntemler."},
            "scope": {"type": "array", "items": {"type": "string"},
                      "description": "Yazabileceği dosya/klasörler (çalışma klasörüne göre). Boş: her yer. Üyelerin "
                      "kapsamları çakışmasın."},
            "model": {"type": "string", "description": "Rol adı (brain: güçlü, fast: hızlı/ucuz, vision) ya da "
                      "sağlayıcı:model. Zor işe brain, basit/mekanik işe fast. Varsayılan brain."},
            "thinking": {"type": "string", "enum": ["off", "low", "medium", "high", "default"],
                         "description": "Düşünme seviyesi: zor tasarım/hata ayıklama high, rutin iş low/off."},
            "isolation": {"type": "string", "enum": ["shared", "worktree", "readonly"],
                          "description": "shared: aynı klasör (scope'lar ayrıysa yeterli) | worktree: kendi git "
                          "dalında ayrı kopya (çakışabilecek büyük değişiklikler; sonra sen birleştirirsin) | "
                          "readonly: sadece inceleme/araştırma."},
            "max_steps": {"type": "integer", "description": "En çok araç adımı. Varsayılan 60."},
        }, ["name", "task"])},
        "to": {"type": "string", "description": "send: üye adı ya da 'all'."},
        "text": {"type": "string", "description": "send: mesaj."},
        "name": {"type": "string", "description": "stop: tek üye. Boş: herkes."},
        "timeout": {"type": "integer", "description": "wait: en çok kaç saniye. Varsayılan 300, en çok 900."},
    }, ["action"])
    risk = READ  # üyelerin her araç çağrısı ayrıca izin kontrolünden geçer

    def summary(self, args: dict[str, Any]) -> str:
        a = args.get("action")
        if a == "start":
            return "takım: " + ", ".join(str(x.get("name")) for x in args.get("agents") or [] if isinstance(x, dict))
        if a == "send":
            return f"{args.get('to')}: {str(args.get('text', ''))[:120]}"
        return str(a)

    def run(self, ctx: ToolContext, action: str, goal: str = "", agents: list[dict[str, Any]] | None = None,
            to: str = "", text: str = "", name: str = "", timeout: int = 300) -> ToolResult:
        from alice.team import Team

        team: Team | None = ctx.state.get("team")
        if action == "start":
            if team is not None and not team.all_done() and not team.stopped:
                return ToolResult("zaten çalışan bir takım var; önce bitmesini bekle ya da action=stop", is_error=True)
            if team is not None:
                team.cleanup()
            if not (goal or "").strip():
                raise ToolError("start için goal gerekli")
            specs = [a for a in agents or [] if isinstance(a, dict)]
            if not specs:
                raise ToolError("start için en az bir üye (agents) gerekli")
            if len(specs) > 8:
                raise ToolError("en çok 8 üye")
            a = ctx.config.agent
            team = Team(ctx, goal.strip(), max_parallel=int(a.get("team_max_parallel", 5)),
                        publish=ctx.state.get("publish"))
            for spec in specs:
                team.add(spec)
            ctx.state["team"] = team
            ctx.state.setdefault("teams", {})[team.id] = team  # Projeler sayfası buradan görür
            team.start()
            return ToolResult(f"takım başladı (proje {team.id}, uygulamada Projeler sayfasında):\n" + team.status_text() +
                              "\n\nŞimdi action=wait ile bekle; sorulara action=send ile cevap ver.")
        if team is None:
            return ToolResult("kurulmuş bir takım yok; önce action=start", is_error=True)
        if action == "status":
            return ToolResult(team.status_text())
        if action == "send":
            target = (to or "").strip().lower()
            if not target:
                raise ToolError("send için to gerekli")
            return ToolResult(team.send("alice", target, text))
        if action == "stop":
            target = (name or "").strip().lower() or None
            if target and target not in team.members:
                raise ToolError(f"takımda '{target}' yok")
            team.stop(target)
            return ToolResult(f"durduruldu: {target or 'herkes'}")
        if action == "wait":
            msgs, finished = team.wait_orchestrator(max(5, min(int(timeout or 300), 900)), ctx.cancelled)
            parts = []
            if msgs:
                parts.append("Sana gelen mesajlar:\n" + "\n".join(m.line() for m in msgs))
            for m in finished:
                head = f"{m.name} {'bitirdi' if m.status == 'done' else m.status}" + (f" (dal {m.branch})" if m.branch else "")
                parts.append(f"{head}. Raporu:\n{m.result}")
            parts.append("Durum:\n" + team.status_text())
            if team.all_done():
                team.cleanup()
                parts.append("Bütün üyeler bitti. Sonuçları birleştir, kendin doğrula ve kullanıcıya özetle.")
            elif not msgs and not finished:
                parts.append("Süre doldu, yeni bir şey yok; tekrar wait ile bekleyebilirsin.")
            return ToolResult("\n\n".join(parts))
        raise ToolError(f"bilinmeyen action: {action}")


TOOLS = [RunPlan(), TaskControl(), TeamTool()]
