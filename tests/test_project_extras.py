from __future__ import annotations

import subprocess
import sys
import threading
import time

from alice.council import Council
from alice.providers.openai_compat import ProviderError
from alice.team import Team, guess_check, list_templates
from tests.conftest import tool_call
from tests.test_council import finding, js, role_of
from tests.test_team import make, steps, who

LEAD_SYS = "projesinin yöneticisisin"


def is_lead(messages) -> bool:
    return LEAD_SYS in messages[0]["content"]


def py(args: str) -> str:
    """run_command için python çağrısı (Windows'ta PowerShell tırnaklı yolu & ile çalıştırır)."""
    exe = f'"{sys.executable}"'
    return f"& {exe} {args}" if sys.platform == "win32" else f"{exe} {args}"


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def git_init(cwd):
    git(cwd, "init", "-q")
    git(cwd, "config", "user.email", "a@b")
    git(cwd, "config", "user.name", "a")


def test_council_retries_once_when_json_missing(workdir):
    asked = {"n": 0}

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role == "critic":
            asked["n"] += 1
            if "sadece o JSON bloğunu" in str(messages[-1]["content"]):
                return js(vote="approve", reason="tamam")
            return "Bence iş iyi, sorun görmedim."  # JSON'u unuttu
        return js(vote="approve")

    agent, _, ctx = make(script, workdir)
    snap = Council(ctx, "work", "iş", "a\n" * 10, depth=1).run()
    critic = next(v for v in snap["votes"] if v["role"] == "critic")
    assert asked["n"] == 2 and critic["last"] == "approve" and snap["result"]["decision"] == "approve"


def test_lead_that_never_plans_gets_a_fallback_worker(workdir):
    def script(model, messages, tools, **kw):
        if is_lead(messages):
            return "Hmm, düşünüyorum."  # hiç araç çağırmıyor
        return "rapor: yaptım"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "küçük bir iş", review=False, council=False)
    team.start(plan=True)
    assert team.wait_until(lambda: "worker" in team.members, 20)
    lead_users = [str(m["content"]) for m in team.lead_agent.messages if m["role"] == "user"]
    assert sum("Henüz ajan kurmadın" in c for c in lead_users) == 2
    assert team.wait_until(lambda: team.members["worker"].status == "done", 10)
    assert any(x["kind"] == "plan" for x in team.timeline)


def test_member_fails_when_no_model_answers(workdir):
    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            raise ProviderError("auth", "anahtar geçersiz")
        return "tamam"

    agent, _, ctx = make(script, workdir)
    seen = []
    team = Team(ctx, "iş", council=False)
    team._notify = lambda text, kind: seen.append(kind)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "failed" and "failed" in seen, 15)
    assert m.result.startswith("Modellere ulaşılamadı: ") and "team:" not in m.result and "anahtar" in m.result


def test_stalled_member_is_reported(workdir):
    gate = threading.Event()

    def script(model, messages, tools, **kw):
        if is_lead(messages):
            return "tamam"
        if steps(messages) == 0:
            return tool_call("list_dir", {})
        gate.wait(10)  # ikinci model çağrısında takılıyor
        return "rapor"

    agent, _, ctx = make(script, workdir, team_stall_minutes=0.01)
    team = Team(ctx, "iş", council=False, review=False)
    team._notify = lambda text, kind: None
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.stall_warned, 15)
    assert team.wait_until(lambda: any("ilerlemiyor" in str(x.get("content")) for x in
                                       (team.lead_agent.messages if team.lead_agent else [])), 10)
    gate.set()
    assert team.wait_until(lambda: m.status == "done", 10)


def test_worktree_conflict_is_handed_back_to_the_agent(workdir):
    git_init(workdir)
    (workdir / "a.txt").write_text("1\n")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-qm", "ilk")
    base = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    state = {"resolved": False, "base": base}

    def script(model, messages, tools, **kw):
        if who(messages) != "backend":
            return "tamam"
        last = str(messages[-1].get("content"))
        if "git merge" in last and not state["resolved"]:
            state["resolved"] = True
            return tool_call("run_command", {"command": f"git merge -X theirs --no-edit {state['base']}"}, "m1")
        if state["resolved"]:
            return "çakışmayı çözdüm"
        return [tool_call("write_file", {"path": "a.txt", "content": "ajan\n"}), "rapor"][min(steps(messages), 1)]

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=False)
    team.add({"name": "backend", "task": "a", "isolation": "worktree"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "review", 10)
    (workdir / "a.txt").write_text("ana dal\n")  # bu arada ana dal da aynı satırı değiştirdi
    git(workdir, "commit", "-qam", "ana dalda değişiklik")
    note = team.review_member("backend", True)
    assert "çakışmayı çözüyor" in note and m.merge_attempts == 1
    assert team.wait_until(lambda: m.status == "review" and state["resolved"], 15)
    assert "birleştirildi" in team.review_member("backend", True)
    assert (workdir / "a.txt").read_text() == "ana dal\n"  # -X theirs: ajan ana dalı kabul etti
    assert any(x["kind"] == "conflict" for x in team.timeline)


def test_run_command_changes_are_tracked_and_revertable(workdir):
    git_init(workdir)
    (workdir / "a.py").write_text("eski\n")
    git(workdir, "add", "-A")
    git(workdir, "commit", "-qm", "ilk")
    (workdir / "kirli.txt").write_text("başta zaten vardı\n")  # proje başında değişmiş: dokunulmaz
    (workdir / "w.py").write_text("open('a.py', 'w').write('yeni\\n')\nopen('b.py', 'w').write('x\\n')\n")
    git(workdir, "add", "w.py")
    git(workdir, "commit", "-qm", "betik")
    cmd = py("w.py")

    def script(model, messages, tools, **kw):
        if who(messages) == "backend":
            return [tool_call("run_command", {"command": cmd}), "rapor"][min(steps(messages), 1)]
        return "tamam"

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=False)
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.status == "review", 10)
    assert sorted(m.files) == ["a.py", "b.py"]
    d = team.diff("backend")
    assert "-eski" in d and "+yeni" in d and "+x" in d
    team.revert("backend")
    assert (workdir / "a.py").read_text() == "eski\n" and not (workdir / "b.py").exists()
    assert (workdir / "kirli.txt").exists()


def test_check_command_lessons_and_rules_reach_the_council(workdir):
    (workdir / "REVIEW.md").write_text("Her public fonksiyonun testi olmalı.\n")
    seen = {}

    def script(model, messages, tools, **kw):
        role = role_of(messages)
        if role:
            seen.setdefault(role, []).append((messages[0]["content"], str(messages[1]["content"])))
            if role == "critic":
                return js(vote="reject", findings=[finding("major", "test eksik")])
            if role == "verifier":
                return js(checks=[{"id": "F1", "status": "confirmed"}], vote="reject")
            return js(vote="reject")
        if is_lead(messages):
            return "tamam"
        return [tool_call("write_file", {"path": "a.py", "content": "x\n"}), "rapor"][min(steps(messages), 1)]

    agent, _, ctx = make(script, workdir)
    team = Team(ctx, "iş", council=True, check=f'{sys.executable} -c "print(\'KONTROL-TAMAM\')"')
    team.add({"name": "backend", "task": "a"})
    team.start()
    m = team.members["backend"]
    assert team.wait_until(lambda: m.councils and m.councils[0].get("state") == "done", 15)
    sys_prompt, material = seen["critic"][0]
    assert "Her public fonksiyonun testi olmalı" in sys_prompt and "KONTROL-TAMAM" in material
    assert any("kontrol komutu" in x["text"] for x in team.timeline)
    assert team.wait_until(lambda: team.lessons == ["test eksik → kontrol ekle"], 10)
    # düzeltme isteğinde ders de ajana gider; ikinci kurulun istemi dersi içerir
    assert team.wait_until(lambda: len(m.councils) >= 2 and m.councils[1].get("state") == "done", 20)
    assert any("test eksik" in s for s, _ in seen["critic"][1:])
    team.stop()


def test_templates_timeline_and_check_guess(workdir):
    agent, _, ctx = make(lambda *a, **k: "rapor" if not is_lead(a[1]) else "tamam", workdir)
    team = Team(ctx, "Şablonluk proje", council=False, check="pytest -q")
    team.add({"name": "backend", "task": "a", "scope": ["src"], "model": "fast"})
    team.start()
    tpl = team.save_template("Benim akışım")
    rows = list_templates()
    assert rows[0]["name"] == "Benim akışım" and rows[0]["agents"][0]["scope"] == ["src"] and rows[0]["check"] == "pytest -q"
    assert tpl["agents"][0]["model"] == "fast"
    team.send("kullanıcı", "backend", "selam")
    kinds = [x["kind"] for x in team.timeline_view()]
    assert kinds[0] == "start" and "msg" in kinds
    (workdir / "tests").mkdir()
    assert guess_check(workdir) == "python -m pytest -q"
    (workdir / "package.json").write_text('{"scripts": {"test": "jest"}}')
    assert guess_check(workdir) == "npm test"
    team.stop()


def test_ask_and_done_notify(workdir):
    agent, _, ctx = make(lambda *a, **k: "tamam", workdir)
    seen = []
    team = Team(ctx, "iş", council=False)
    team._notify = lambda text, kind: seen.append((kind, text))
    team.ask_user("Hangisi?")
    team.finish("özet")
    assert [k for k, _ in seen] == ["ask", "done"]


def test_member_output_streams_live(workdir):
    from alice.agent import Agent, NullUI
    from alice.permissions import Permissions
    from alice.providers.router import Router
    from alice.tools import all_tools
    from alice.tools.base import ToolContext
    from tests.conftest import FakeProvider, make_config

    class Streaming(FakeProvider):
        def chat_stream(self, model, messages, on_delta, cancel=None, on_reasoning=None, **kw):
            res = self.chat(model, messages, **kw)
            if on_reasoning:
                on_reasoning("önce dosyaya bakayım")
            for i in range(0, len(res.text), 4):  # gerçek model gibi yavaş yavaş
                on_delta(res.text[i:i + 4])
                time.sleep(0.05)
            return res

    def script(model, messages, tools, **kw):
        return "tamam" if is_lead(messages) else "Rapor: her şey yolunda, testler geçti."

    cfg = make_config({"brain": ["a:m"], "fast": ["a:m"], "vision": ["a:m"]})
    prov = Streaming("a", script)
    router = Router(cfg, provider_factory=lambda pc: prov, sleep=lambda s: None)
    ctx = ToolContext(cwd=workdir, config=cfg, router=router)
    Agent(cfg, router, all_tools(), ctx, Permissions("auto"), NullUI(), system_prompt="sys")
    events = []
    team = Team(ctx, "iş", council=False, review=False, publish=events.append)
    team._notify = lambda text, kind: None
    team.add({"name": "backend", "task": "a"})
    team.start()
    assert team.wait_until(lambda: team.members["backend"].status == "done", 10)
    live = [e for e in events if e["type"] == "team_live" and e["name"] == "backend"]
    assert any(e["text"].startswith("Rapor") for e in live)  # metin yazılırken parça parça geldi
    assert any(e["think"] == "önce dosyaya bakayım" for e in live)
    assert live[-1]["text"] == "" and live[-1]["think"] == ""  # bitince canlı kutu kapandı
    team.stop()
