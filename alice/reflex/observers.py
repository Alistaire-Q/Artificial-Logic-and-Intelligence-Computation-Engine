"""Reflex döngüsü için gözlemciler: ekranı ID'li aday listesine çevirip aksiyonu uygular."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from alice.tools.base import ToolContext

EDITABLE_TAGS = {"input", "textarea"}
EDITABLE_ROLES = {"textbox", "searchbox", "combobox", "Edit", "ComboBox", "Document"}


@dataclass
class Element:
    id: int
    kind: str
    label: str
    editable: bool
    value: str = ""
    in_view: bool = True

    def describe(self) -> str:
        s = f"{self.kind} '{self.label}'"
        if self.value:
            s += f" (şu an: '{self.value}')"
        if not self.in_view:
            s += " [aşağıda]"
        return s


@dataclass
class Observation:
    title: str
    location: str
    elements: list[Element]


class Observer(Protocol):
    kind: str

    def observe(self) -> Observation: ...
    def click(self, element_id: int) -> None: ...
    def type(self, element_id: int, text: str, submit: bool) -> None: ...
    def scroll(self, down: bool) -> None: ...
    def back(self) -> None: ...
    def press_enter(self) -> None: ...


class BrowserObserver:
    kind = "browser"

    def __init__(self, ctx: ToolContext, start_url: str | None = None):
        from alice.tools.browser import get_browser

        self.ctx = ctx
        self.b = get_browser(ctx)
        if start_url:
            if "://" not in start_url:
                start_url = "https://" + start_url
            self.b.page.goto(start_url, wait_until="domcontentloaded", timeout=45000)

    def observe(self) -> Observation:
        try:
            self.b.page.wait_for_load_state("domcontentloaded", timeout=6000)
        except Exception:  # noqa: BLE001
            pass
        items = self.b.snapshot(limit=150)
        els = []
        for i in items:
            kind = i["role"] or i["tag"]
            if i["tag"] == "input" and i["type"]:
                kind = f"input[{i['type']}]"
            editable = (i["tag"] in EDITABLE_TAGS and i["type"] not in ("submit", "button", "checkbox", "radio")) \
                or i["role"] in EDITABLE_ROLES
            els.append(Element(i["id"], kind, i["text"] or i["placeholder"] or i["href"], editable, i["value"], i["inView"]))
        return Observation(self.b.page.title(), self.b.page.url, els)

    def click(self, element_id: int) -> None:
        loc = self.b.locator(element_id)
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=8000)
        self.b.page.wait_for_timeout(500)

    def type(self, element_id: int, text: str, submit: bool) -> None:
        loc = self.b.locator(element_id)
        loc.fill(text, timeout=8000)
        if submit:
            loc.press("Enter")
            self.b.page.wait_for_timeout(900)

    def scroll(self, down: bool) -> None:
        self.b.page.mouse.wheel(0, 700 if down else -700)
        self.b.page.wait_for_timeout(300)

    def back(self) -> None:
        self.b.page.go_back(wait_until="domcontentloaded")

    def press_enter(self) -> None:
        self.b.page.keyboard.press("Enter")
        self.b.page.wait_for_timeout(900)


class DesktopObserver:
    kind = "desktop"

    def __init__(self, ctx: ToolContext, window_title: str | None = None):
        self.ctx = ctx
        self.title = window_title

    def observe(self) -> Observation:
        from alice.tools.desktop import collect_ui_elements

        title, items = collect_ui_elements(self.ctx, self.title, limit=150)
        els = [Element(i["id"], i["role"], i["name"] or i["auto_id"], i["role"] in EDITABLE_ROLES, i["value"])
               for i in items if i["enabled"]]
        return Observation(title, "desktop", els)

    def click(self, element_id: int) -> None:
        from alice.tools.desktop import ui_act

        ui_act(self.ctx, element_id, "click")

    def type(self, element_id: int, text: str, submit: bool) -> None:
        from alice.tools.computer import press_keys
        from alice.tools.desktop import ui_act

        ui_act(self.ctx, element_id, "set_text", text)
        if submit:
            press_keys("enter")

    def scroll(self, down: bool) -> None:
        import pyautogui

        pyautogui.scroll(-600 if down else 600)

    def back(self) -> None:
        from alice.tools.computer import press_keys

        press_keys("alt+left")

    def press_enter(self) -> None:
        from alice.tools.computer import press_keys

        press_keys("enter")
