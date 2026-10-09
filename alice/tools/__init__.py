from __future__ import annotations

from alice.tools import browser, computer, core, desktop, extras, flight, files, meta, shell, system, web
from alice.tools.base import Tool, ToolContext, ToolError, ToolResult


def all_tools() -> list[Tool]:
    return [*files.TOOLS, *shell.TOOLS, *web.TOOLS, *system.TOOLS, *computer.TOOLS, *desktop.TOOLS,
            *browser.TOOLS, *meta.TOOLS, *core.TOOLS, *flight.TOOLS, *extras.TOOLS]


def attach_mcp(agent, ctx: ToolContext, emit=lambda s: None) -> int:
    """config'teki MCP sunucularını başlat ve araçlarını ajana ekle."""
    from alice.mcp import load_mcp

    servers, tools = load_mcp(ctx.config, emit)
    ctx.state["mcp_servers"] = servers
    for t in tools:
        agent.tools[t.name] = t
    return len(tools)


def cleanup(ctx: ToolContext) -> None:
    shell.cleanup(ctx)
    st = ctx.state.pop("core_store", None)
    if st is not None:
        st.close()
    browser.cleanup(ctx)
    from alice.mcp import close_all

    close_all(ctx.state.pop("mcp_servers", []))


__all__ = ["Tool", "ToolContext", "ToolError", "ToolResult", "all_tools", "attach_mcp", "cleanup"]
