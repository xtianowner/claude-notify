#!/usr/bin/env python3
"""R32 MCP 辅助通道：Claude Desktop 把"任务完成 / 里程碑"主动汇报给 claude-notify。

motivation
  AX 桥接（R30）能感知"生成中 / 等输入"等 UI 状态，但拿不到原文摘要。
  这个 MCP server 暴露 notify_progress(title, summary, level) 工具，Claude 在 system
  prompt 引导下主动调用 —— 弥补 AX 只能读 UI 状态不能读语义的缺口。

安装（macOS Claude Desktop）：
  1. 在 ~/Library/Application Support/Claude/claude_desktop_config.json 加：
     {
       "mcpServers": {
         "claude-notify": {
           "command": "/Users/tian/miniconda3/bin/python3",
           "args": ["/绝对路径/scripts/mcp_notify_server.py"]
         }
       }
     }
  2. 重启 Claude Desktop。
  3. 验证：在 Claude Desktop 对话里说"调一下 notify_progress 工具汇报当前进度"，
     Claude 应该会弹工具调用，确认后 claude-notify dashboard 会出现一条 Notification 事件。

引导话术（建议放进 Project Knowledge / system prompt）：
  「每次完成一个里程碑、卡在用户决策点、需要等待长时间任务时，调用 notify_progress
   工具。title 给一句话主题，summary 给当前进度，level 用 'info'/'milestone'/'blocked'。」

依赖：mcp >= 1.0
  pip install mcp
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("[fatal] 需要 pip install mcp", file=sys.stderr)
    sys.exit(1)


BACKEND_URL = os.environ.get("CLAUDE_NOTIFY_URL", "http://127.0.0.1:8787/api/event")


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _post_event(payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BACKEND_URL, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            return {"ok": True, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


server = Server("claude-notify")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="notify_progress",
            description=(
                "Send a progress / milestone notification to the user's claude-notify "
                "dashboard (and Feishu / browser push). Call this when you complete a "
                "milestone, get blocked on a user decision, or finish a long task — so "
                "the user can see what's happening without staring at the chat. "
                "Title is a short headline; summary is one-line context; level is one of "
                "'info' (default), 'milestone' (notable progress), 'blocked' (need user input)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short headline (≤40 chars)"},
                    "summary": {"type": "string", "description": "One-line progress detail (≤200 chars)"},
                    "level": {
                        "type": "string",
                        "enum": ["info", "milestone", "blocked"],
                        "default": "info",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional: stable ID for this conversation (re-use across calls).",
                    },
                },
                "required": ["title"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "notify_progress":
        return [TextContent(type="text", text=f"unknown tool: {name}")]

    title = (arguments.get("title") or "").strip()[:120]
    summary = (arguments.get("summary") or "").strip()[:400]
    level = (arguments.get("level") or "info").strip()
    conv_id = (arguments.get("conversation_id") or "").strip() or "mcp-default"

    # 映射到 claude-notify 事件格式（source=desktop_app 复用 normalizer）
    # level → event_name：blocked → Notification；milestone/info → Stop（回合完成）
    if level == "blocked":
        event_name = "Notification"
        msg = f"[blocked] {title}: {summary}" if summary else f"[blocked] {title}"
    elif level == "milestone":
        event_name = "Stop"
        msg = f"[milestone] {title}: {summary}" if summary else f"[milestone] {title}"
    else:
        event_name = "Stop"
        msg = f"{title}: {summary}" if summary else title

    evt = {
        "ts": _now_iso(),
        "source": "desktop_app",
        "session_id": f"mcp-{conv_id}",
        "event": event_name,
        "project": "Claude Desktop (MCP)",
        "window_title": title,
        "window_id": conv_id,
        "conversation_id": conv_id,
        "message": msg,
        "last_assistant_message": summary or title,
        "raw": {
            "via": "mcp",
            "level": level,
            "title": title,
            "summary": summary,
        },
    }
    result = await asyncio.to_thread(_post_event, evt)
    if result.get("ok"):
        return [TextContent(type="text",
                            text=f"Notification sent to claude-notify dashboard (event={event_name}).")]
    return [TextContent(type="text",
                        text=f"Failed to reach claude-notify backend at {BACKEND_URL}: {result.get('error')}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
