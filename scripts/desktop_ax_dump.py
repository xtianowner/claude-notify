#!/usr/bin/env python3
"""R29 POC：dump Claude Desktop 所有窗口的 Accessibility 树。

用法：
  python3 scripts/desktop_ax_dump.py            # 列窗口 + 浅层
  python3 scripts/desktop_ax_dump.py --deep     # 递归全树（量大）
  python3 scripts/desktop_ax_dump.py --grep stop  # 只显示 label/role 含 "stop" 的节点

前置：
  - macOS
  - 已装 pyobjc-framework-ApplicationServices
  - 当前 python 解释器在「系统设置 → 隐私与安全 → 辅助功能」里勾选

目的：跑一次拿到 Claude.app 真实 AX 节点结构（role / title / value / actions）
作为 R30 状态机启发式的输入。
"""
from __future__ import annotations
import argparse
import subprocess
import sys

try:
    from ApplicationServices import (
        AXIsProcessTrusted,
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeNames,
        AXUIElementCopyAttributeValue,
        AXUIElementCopyActionNames,
    )
except ImportError as e:
    print(f"[fatal] pyobjc-framework-ApplicationServices 未安装：{e}", file=sys.stderr)
    sys.exit(1)


CLAUDE_BUNDLE_HINT = "Claude"  # 进程名匹配


def find_claude_pid() -> int | None:
    """ps 找主进程：/Applications/Claude.app/Contents/MacOS/Claude（无 Helper / 无 type=）"""
    try:
        out = subprocess.check_output(["ps", "-Ao", "pid,command"], text=True)
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if "/Applications/Claude.app/Contents/MacOS/Claude" not in line:
            continue
        if "Helper" in line or "--type=" in line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 1:
            continue
        try:
            return int(parts[0])
        except ValueError:
            continue
    return None


def ax_attr(el, name: str):
    try:
        err, val = AXUIElementCopyAttributeValue(el, name, None)
        if err != 0:
            return None
        return val
    except Exception:
        return None


def ax_attr_names(el) -> list[str]:
    try:
        err, names = AXUIElementCopyAttributeNames(el, None)
        if err != 0 or names is None:
            return []
        return list(names)
    except Exception:
        return []


def ax_actions(el) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(el, None)
        if err != 0 or names is None:
            return []
        return list(names)
    except Exception:
        return []


def short(s, n=80):
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "…"


def describe(el, depth: int) -> str:
    role = ax_attr(el, "AXRole") or ""
    subrole = ax_attr(el, "AXSubrole") or ""
    title = ax_attr(el, "AXTitle") or ""
    desc = ax_attr(el, "AXDescription") or ""
    value = ax_attr(el, "AXValue")
    help_ = ax_attr(el, "AXHelp") or ""
    ident = ax_attr(el, "AXIdentifier") or ""
    enabled = ax_attr(el, "AXEnabled")
    actions = ax_actions(el)
    indent = "  " * depth
    parts = [f"{indent}[{role}]"]
    if subrole:
        parts.append(f"sub={subrole}")
    if title:
        parts.append(f"title={short(title, 60)!r}")
    if desc:
        parts.append(f"desc={short(desc, 60)!r}")
    if ident:
        parts.append(f"id={short(ident, 30)!r}")
    if help_:
        parts.append(f"help={short(help_, 40)!r}")
    if value is not None and not isinstance(value, (list, tuple)):
        v = short(value, 50)
        if v:
            parts.append(f"value={v!r}")
    if enabled is False:
        parts.append("disabled")
    if actions:
        parts.append(f"actions={actions}")
    return " ".join(parts)


def walk(el, depth: int, max_depth: int, grep: str | None, hit_only: bool):
    line = describe(el, depth)
    if grep:
        if grep.lower() in line.lower():
            print(line)
        # 哪怕没 match 也要递归（子节点可能 match）
    elif not hit_only:
        print(line)
    if depth >= max_depth:
        return
    children = ax_attr(el, "AXChildren") or []
    for child in children:
        walk(child, depth + 1, max_depth, grep, hit_only)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="递归全树（默认 4 层）")
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--grep", default=None, help="仅显示含此子串的行（递归仍走）")
    ap.add_argument("--pid", type=int, default=None, help="指定 Claude.app PID（默认自动找）")
    args = ap.parse_args()

    if not AXIsProcessTrusted():
        print("[warn] 当前 python 没有辅助功能权限。", file=sys.stderr)
        print("       去 系统设置 → 隐私与安全 → 辅助功能 加入 python 解释器后重跑。", file=sys.stderr)
        print(f"       当前解释器：{sys.executable}", file=sys.stderr)
        # 不直接 exit，让用户能看到一行 fatal 即返回
        sys.exit(2)

    pid = args.pid or find_claude_pid()
    if not pid:
        print("[fatal] 没找到 Claude.app 主进程。请先打开 Claude Desktop。", file=sys.stderr)
        sys.exit(3)
    print(f"[info] Claude.app pid = {pid}")

    app = AXUIElementCreateApplication(pid)
    windows = ax_attr(app, "AXWindows") or []
    print(f"[info] windows = {len(windows)}")
    if not windows:
        # 也可能 app 当前隐藏 / 没窗口；试 main / focused
        for k in ("AXMainWindow", "AXFocusedWindow"):
            w = ax_attr(app, k)
            if w:
                print(f"[info] fallback {k} found")
                windows = [w]
                break

    max_depth = 999 if args.deep else args.max_depth
    for i, win in enumerate(windows):
        title = ax_attr(win, "AXTitle") or "(no title)"
        print(f"\n=== window {i}: {title} ===")
        walk(win, 0, max_depth, args.grep, hit_only=False)


if __name__ == "__main__":
    main()
