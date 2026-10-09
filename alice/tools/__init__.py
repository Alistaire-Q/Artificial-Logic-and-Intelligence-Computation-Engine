from __future__ import annotations

from alice.tools import browser, computer, desktop, files, meta, shell, system, web
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult


def all_tools() -> list[Tool]:
    return [*files.TOOLS, *shell.TOOLS, *web.TOOLS, *system.TOOLS, *computer.TOOLS, *desktop.TOOLS,
            *browser.TOOLS, *meta.TOOLS]


def cleanup(ctx: ToolContext) -> None:
    shell.cleanup(ctx)
    browser.cleanup(ctx)


__all__ = ["Tool", "ToolContext", "ToolError", "ToolResult", "all_tools", "cleanup"]
