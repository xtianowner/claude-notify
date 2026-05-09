#!/usr/bin/env python3
"""把 claude-notify 的 4 条 hook 写入 ~/.claude/settings.json。

用法：
  python scripts/install-hooks.py            # 安装
  python scripts/install-hooks.py --uninstall  # 卸载
  python scripts/install-hooks.py --dry-run  # 只打印将要写入的 settings.json，不落盘
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_PY = Path(__file__).resolve().parent / "hook-notify.py"

EVENTS_NORMAL = ["Notification", "Stop", "SubagentStop", "SessionStart", "SessionEnd"]
EVENT_HEARTBEAT = "PreToolUse"
HEARTBEAT_FLAG = "--heartbeat"


def _cmd(heartbeat: bool = False) -> str:
    cmd = f"python3 {HOOK_PY}"
    if heartbeat:
        cmd += f" {HEARTBEAT_FLAG}"
    return cmd


def _is_our_hook(hook_obj: dict) -> bool:
    cmd = (hook_obj or {}).get("command") or ""
    return "hook-notify.py" in cmd


def _ensure_event_array(hooks_root: dict, event_name: str) -> list:
    arr = hooks_root.get(event_name)
    if not isinstance(arr, list):
        arr = []
        hooks_root[event_name] = arr
    return arr


def _add_or_replace(matchers: list, command: str) -> bool:
    """在 matcher 列表里找到我们的 hook（按 hook-notify.py 关键字识别），替换 command；找不到就新增"""
    for matcher in matchers:
        inner = (matcher or {}).get("hooks") or []
        for h in inner:
            if _is_our_hook(h):
                h["command"] = command
                h["type"] = "command"
                return False
    matchers.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _remove_ours(matchers: list) -> int:
    removed = 0
    for matcher in matchers:
        inner = (matcher or {}).get("hooks") or []
        keep = [h for h in inner if not _is_our_hook(h)]
        removed += len(inner) - len(keep)
        matcher["hooks"] = keep
    matchers[:] = [m for m in matchers if (m.get("hooks") or [])]
    return removed


def load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text("utf-8"))
    except Exception as e:
        print(f"!! settings.json 解析失败: {e}", file=sys.stderr)
        sys.exit(2)


def write_settings(data: dict, dry_run: bool):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if dry_run:
        print("=== DRY RUN: settings.json 将变成 ===")
        print(text)
        return
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = SETTINGS.with_suffix(f".json.bak-{ts}")
        shutil.copy2(SETTINGS, backup)
        print(f"已备份原 settings.json → {backup}")
    SETTINGS.write_text(text, "utf-8")
    print(f"已写入 {SETTINGS}")


def install(dry_run: bool):
    data = load_settings()
    hooks_root = data.setdefault("hooks", {})
    cmd_normal = _cmd(False)
    cmd_hb = _cmd(True)
    summary = []
    for ev in EVENTS_NORMAL:
        arr = _ensure_event_array(hooks_root, ev)
        added = _add_or_replace(arr, cmd_normal)
        summary.append(f"  [{ev}] {'+ 新增' if added else '~ 已存在/已更新 command'}")
    arr = _ensure_event_array(hooks_root, EVENT_HEARTBEAT)
    added = _add_or_replace(arr, cmd_hb)
    summary.append(f"  [{EVENT_HEARTBEAT}] {'+ 新增' if added else '~ 已存在/已更新 command'}（心跳，不推送）")
    print("即将注册以下 hooks 到 ~/.claude/settings.json：")
    print("\n".join(summary))
    write_settings(data, dry_run)


def uninstall(dry_run: bool):
    data = load_settings()
    hooks_root = data.get("hooks") or {}
    total = 0
    for ev in EVENTS_NORMAL + [EVENT_HEARTBEAT]:
        arr = hooks_root.get(ev)
        if not isinstance(arr, list):
            continue
        n = _remove_ours(arr)
        total += n
        if not arr:
            hooks_root.pop(ev, None)
    print(f"将移除 {total} 条 claude-notify hook。")
    write_settings(data, dry_run)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not HOOK_PY.exists():
        print(f"!! hook 脚本不存在: {HOOK_PY}", file=sys.stderr)
        sys.exit(2)

    if args.uninstall:
        uninstall(args.dry_run)
    else:
        install(args.dry_run)


if __name__ == "__main__":
    main()
