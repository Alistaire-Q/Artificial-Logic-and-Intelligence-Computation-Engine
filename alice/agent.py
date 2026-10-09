"""Ana ajan döngüsü (Claude Code / Codex tarzı).

user mesajı -> model (araçlarla) -> tool_calls varsa izin kontrolü + çalıştır ->
sonuçları geri ver -> model artık araç çağırmayınca tur biter.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from alice.config import Config
from alice.decider import Decider
from alice.permissions import Permissions
from alice.prompts import COMPACT_PROMPT, build_system_prompt
from alice.providers.openai_compat import ChatResult
from alice.providers.router import AllProvidersFailed, Router, estimate_tokens, strip_images
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult, truncate


class UI(Protocol):
    def assistant_text(self, text: str, source: str) -> None: ...
    def tool_start(self, name: str, summary: str) -> None: ...
    def tool_end(self, name: str, result: ToolResult) -> None: ...
    def ask_permission(self, name: str, summary: str, risk: str) -> str: ...  # y | n | a
    def info(self, msg: str) -> None: ...
    def thinking(self, label: str) -> Any: ...  # context manager


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NullUI:
    def __init__(self, approve: str = "y"):
        self.approve = approve
        self.events: list[tuple[str, Any]] = []

    def assistant_text(self, text, source):
        self.events.append(("text", text))

    def tool_start(self, name, summary):
        self.events.append(("tool", name))

    def tool_end(self, name, result):
        self.events.append(("result", result.text))

    def ask_permission(self, name, summary, risk):
        self.events.append(("ask", name))
        return self.approve

    def info(self, msg):
        self.events.append(("info", msg))

    def thinking(self, label):
        return _Null()


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        # Bazı modeller sona fazladan metin ya da code fence ekliyor.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    return val


class Agent:
    def __init__(
        self,
        config: Config,
        router: Router,
        tools: list[Tool],
        ctx: ToolContext,
        permissions: Permissions,
        ui: UI | None = None,
        decider: Decider | None = None,
        system_prompt: str | None = None,
        is_child: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = {t.name: t for t in tools if not (is_child and t.name == "task")}
        self.ctx = ctx
        self.permissions = permissions
        self.ui = ui or NullUI()
        self.decider = decider
        self.is_child = is_child
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt or build_system_prompt(ctx.cwd)}]
        a = config.agent
        self.max_steps = int(a.get("max_steps", 80))
        self.compact_at = int(a.get("compact_at_tokens", 48000))
        self.output_limit = int(a.get("tool_output_chars", 12000))
        self.keep_images = int(a.get("keep_images", 2))
        if not is_child:
            ctx.state["agent"] = self

    # ------------------------------------------------------------------ public
    def tool_specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values()]

    def spawn_child(self) -> "Agent":
        return Agent(self.config, self.router, list(self.tools.values()), self.ctx, self.permissions, self.ui,
                     self.decider, system_prompt=self.messages[0]["content"], is_child=True)

    def run_turn(self, user_text: str, max_steps: int | None = None) -> str:
        self.messages.append({"role": "user", "content": user_text})
        final = ""
        steps = max_steps or self.max_steps
        try:
            for _ in range(steps):
                self._maybe_compact()
                self._prune_images()
                with self.ui.thinking("düşünüyor"):
                    res = self.router.chat("brain", self.messages, tools=self.tool_specs())
                msg = res.message
                self.messages.append(msg)
                if msg.get("content"):
                    final = msg["content"]
                    self.ui.assistant_text(msg["content"], f"{res.provider}:{res.model}")
                if not msg.get("tool_calls"):
                    return final
                self._run_tools(msg["tool_calls"])
            self.ui.info(f"adım limiti ({steps}) doldu; devam etmek için yeni mesaj yaz")
            return final
        except KeyboardInterrupt:
            self._repair_after_interrupt()
            self.ui.info("durduruldu")
            return final
        except AllProvidersFailed as e:
            self._repair_after_interrupt()
            self.ui.info(str(e))
            return final

    # ------------------------------------------------------------------- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        pending_images: list[tuple[str, list[str]]] = []
        for call in calls:
            name = call["function"]["name"]
            result = self._run_one(call)
            text = truncate(result.text, self.output_limit)
            if result.is_error:
                text = f"HATA: {text}"
            if result.images:
                if self._brain_sees_images():
                    pending_images.append((name, result.images))
                else:
                    text += "\n\n[görüntü açıklaması - vision modeli]\n" + self._describe_images(result.images)
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": text})
        for name, images in pending_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": f"[{name} aracının döndürdüğü görüntü]"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            self.messages.append({"role": "user", "content": parts, "_image": True})

    def _run_one(self, call: dict[str, Any]) -> ToolResult:
        name = call["function"]["name"]
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"bilinmeyen araç '{name}'. Mevcut araçlar: {', '.join(self.tools)}", is_error=True)
        try:
            args = parse_args(call["function"].get("arguments"))
        except json.JSONDecodeError as e:
            return ToolResult(f"argümanlar geçerli JSON değil ({e}). Tekrar dene.", is_error=True)
        summary = tool.summary(args)
        verdict = self.permissions.decide(tool, args, self.ctx)
        if verdict == "ask":
            ans = self.ui.ask_permission(name, summary, tool.risk_for(args))
            if ans == "a":
                self.permissions.always_allow.add(name)
            elif ans != "y":
                return ToolResult("Kullanıcı bu çağrıyı reddetti. Aynı şeyi tekrar deneme; başka yol bul ya da kullanıcıya sor.",
                                  is_error=True)
        self.ui.tool_start(name, summary)
        try:
            result = tool.run(self.ctx, **args)
        except ToolError as e:
            result = ToolResult(str(e), is_error=True)
        except TypeError as e:
            result = ToolResult(f"geçersiz parametre: {e}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - araç hatası ajanı düşürmesin, modele dönsün
            result = ToolResult(f"{type(e).__name__}: {e}", is_error=True)
        self.ui.tool_end(name, result)
        return result

    # ----------------------------------------------------------------- vision
    def _brain_sees_images(self) -> bool:
        ref = self.router.first_available("brain")
        return bool(ref and self.router.supports_vision(ref))

    def _describe_images(self, images: list[str]) -> str:
        task = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        parts: list[dict[str, Any]] = [{"type": "text", "text": (
            "You are the eyes of a computer-use agent. Describe this screenshot precisely for the task below: "
            "visible windows and their titles, focused element, all buttons/links/fields/menus with their exact text "
            "and approximate pixel coordinates (x,y of center in this image), dialogs, errors, and anything relevant. "
            f"Task: {task[:800]}")}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        try:
            res = self.router.chat("vision", [{"role": "user", "content": parts}], max_tokens=1500)
            return res.text
        except (AllProvidersFailed, KeyError) as e:
            return f"(görüntü açıklanamadı: {str(e)[:200]})"

    def _prune_images(self) -> None:
        idx = [i for i, m in enumerate(self.messages) if m.get("_image")]
        for i in idx[:-self.keep_images] if self.keep_images else idx:
            self.messages[i] = {"role": "user", "content": "[eski ekran görüntüsü yer kazanmak için kaldırıldı]", "_image": False}

    # -------------------------------------------------------------- context
    def _maybe_compact(self, force: bool = False) -> bool:
        total = estimate_tokens(self.messages, self.tool_specs())
        if not force and total < self.compact_at:
            return False
        budget = int(self.compact_at * 0.35)
        users = [i for i, m in enumerate(self.messages) if i > 0 and m["role"] == "user" and not m.get("_image")]
        if not users:
            return False
        cut = users[-1]
        for i in reversed(users):
            if estimate_tokens(self.messages[i:]) <= budget:
                cut = i
            else:
                break
        old = self.messages[1:cut]
        if not old:
            # Tek turda bile çok büyümüş: büyük araç çıktılarını kısalt.
            for m in self.messages[cut:]:
                if m["role"] == "tool" and len(m["content"]) > 3000:
                    m["content"] = truncate(m["content"], 3000)
            return False
        transcript = []
        for m in strip_images(old):
            c = m.get("content") or ""
            if m.get("tool_calls"):
                c += "\n" + "\n".join(f"-> {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
            transcript.append(f"[{m['role']}] {truncate(c, 2500)}")
        self.ui.info("context büyüdü, özetleniyor...")
        try:
            res: ChatResult = self.router.chat("brain", [
                {"role": "system", "content": COMPACT_PROMPT},
                {"role": "user", "content": "\n\n".join(transcript)[-120000:]},
            ], max_tokens=2000)
        except AllProvidersFailed as e:
            self.ui.info(f"özetleme başarısız: {e}")
            return False
        self.messages = [self.messages[0],
                         {"role": "user", "content": f"[Önceki konuşmanın özeti]\n{res.text}"},
                         {"role": "assistant", "content": "Özeti aldım, kaldığım yerden devam ediyorum."},
                         *self.messages[cut:]]
        return True

    def compact(self) -> bool:
        return self._maybe_compact(force=True)

    def _repair_after_interrupt(self) -> None:
        """Yarım kalan tool_calls için sahte sonuç ekle, yoksa sonraki istek 400 alır."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                for tc in m["tool_calls"]:
                    if tc["id"] not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                                              "content": "durduruldu (kullanıcı kesti)"})
                break

    def reset(self) -> None:
        self.messages = self.messages[:1]
