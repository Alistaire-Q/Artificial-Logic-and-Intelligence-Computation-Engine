"""Çok adımlı görev araçları: run_plan (planla, yürüt, doğrula) ve task_control (onay, durdurma, durum)."""

from __future__ import annotations

from typing import Any

from alice.tools.base import COMPUTER, READ, Tool, ToolContext, ToolError, ToolResult, obj


class RunPlan(Tool):
    name = "run_plan"
    description = (
        "Birkaç ayrı adım süren bir isteği kendin planlayıp baştan sona yap: istek 2-8 adıma bölünür, her adım "
        "sırayla araçlarla yapılır ve sonucu ayrıca kontrol edilir; olmayan adım bir kez başka yoldan denenir. "
        "Geri alınamaz adımlardan (mesaj/e-posta göndermek, silmek, satın almak, kurmak) önce kullanıcıya onay "
        "sorulur. Ör. 'Gmail'deki son faturayı indir, masaüstüne kaydet ve Ahmet'e WhatsApp'tan gönder', "
        "'yarınki hava durumuna bak, yağmur varsa takvime not ekle'. goal'e kullanıcının istediğini eksiksiz, "
        "adlarıyla yaz. Tek adımlık işler için doğrudan ilgili aracı kullan."
    )
    parameters = obj({"goal": {"type": "string", "description": "kullanıcının isteği, eksiksiz"}}, ["goal"])
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
        "Süren run_plan görevini yönet. Görev onay isteyince kullanıcı 'evet' derse action=approve, 'hayır' derse "
        "action=reject; 'dur', 'vazgeç' derse action=stop; 'ne durumda' derse action=status. Onayı kullanıcı "
        "söylemeden asla verme."
    )
    parameters = obj({"action": {"type": "string", "enum": ["approve", "reject", "stop", "status"]}}, ["action"])
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


TOOLS = [RunPlan(), TaskControl()]
