#!/usr/bin/env python3
"""claude-notify 一键升级：git pull + pip install + 提示重启。

用法：
  python3 scripts/update.py                # 标准升级（不自动重启 backend）
  python3 scripts/update.py --restart      # 升级后自动重启 backend
  python3 scripts/update.py --dry-run      # 只打印命令不执行
  python3 scripts/update.py --skip-deps    # 跳过 pip install（已知 deps 不变时）

设计原则：
  - 永不丢用户数据：data/ 在 .gitignore 内，pull 不会动；config 走 _merge 自动 merge 新默认字段
  - 永不破坏 hook：脚本不动 ~/.claude/settings.json（如需重装 hook，用户手动跑 install-hooks.py）
  - 永不自动 force：工作树脏时拒绝继续，提示用户先 commit/stash
  - macOS 自动 pyobjc：检测平台装 backend/requirements-desktop.txt（Linux 跳过）
  - 升级摘要：打印从 before HEAD 到 after HEAD 的 commit 列表，用户一眼看清改了什么
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQ_BASE = PROJECT_ROOT / "backend" / "requirements.txt"
REQ_DESKTOP = PROJECT_ROOT / "backend" / "requirements-desktop.txt"


def _print(msg: str, kind: str = "info") -> None:
    icons = {"info": "→ ", "ok": "✅ ", "warn": "⚠ ", "err": "✖ ", "step": "▶ "}
    print(f"{icons.get(kind, '')}{msg}")


def _run(cmd, dry: bool = False, check: bool = True, capture: bool = True) -> str:
    """跑命令并打印。dry=True 时只打印不执行。返回 stdout。"""
    cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
    print(f"  $ {cmd_str}")
    if dry:
        return ""
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT),
                           shell=isinstance(cmd, str))
        if r.stdout.strip():
            for line in r.stdout.splitlines():
                print(f"    {line}")
    else:
        r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), shell=isinstance(cmd, str))
    if r.returncode != 0:
        if capture and r.stderr:
            print(f"    [stderr] {r.stderr.strip()[:500]}", file=sys.stderr)
        if check:
            sys.exit(r.returncode)
    return getattr(r, "stdout", "") or ""


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _backend_running_pids() -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "uvicorn backend.app"],
            stderr=subprocess.DEVNULL, text=True,
        )
        return [int(p) for p in out.split() if p.isdigit()]
    except subprocess.CalledProcessError:
        return []


def _restart_backend(dry: bool = False) -> None:
    pids = _backend_running_pids()
    if pids:
        _print(f"停止旧 backend 进程 (pids={pids})", "step")
        if not dry:
            for p in pids:
                try:
                    os.kill(p, 15)  # SIGTERM
                except Exception:
                    pass
            time.sleep(1)
    _print("启动新 backend (后台 nohup)", "step")
    log_path = "/tmp/claude-notify.log"
    port = os.environ.get("PORT", "8787")
    host = os.environ.get("HOST", "127.0.0.1")
    if dry:
        print(f"  $ nohup {sys.executable} -m uvicorn backend.app:app --host {host} --port {port} > {log_path} 2>&1 &")
        return
    log_fd = open(log_path, "ab")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app",
         "--host", host, "--port", port],
        stdout=log_fd, stderr=subprocess.STDOUT,
        start_new_session=True, cwd=str(PROJECT_ROOT),
    )
    time.sleep(2)
    _print(f"backend 启动中，日志：tail -f {log_path}", "ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--restart", action="store_true", help="升级完自动重启 backend")
    ap.add_argument("--skip-deps", action="store_true", help="跳过 pip install")
    ap.add_argument("--branch", default="main", help="拉取的分支，默认 main")
    args = ap.parse_args()

    _print(f"claude-notify update — branch={args.branch} dry-run={args.dry_run}", "step")

    # 1. 工作树干净？
    # 区分：modified/staged → 拒绝（pull 可能冲突）；untracked → warn 继续
    _print("检查 git 工作树", "step")
    status = _run(["git", "status", "--porcelain"], check=False)
    modified = [l for l in status.splitlines() if l and not l.startswith("??")]
    untracked = [l for l in status.splitlines() if l.startswith("??")]
    if modified:
        _print("工作树有未提交的本地修改，拒绝升级（避免 pull 冲突丢改动）：", "err")
        for line in modified:
            print(f"    {line}")
        print("\n  请先 commit / stash 后再跑 update：")
        print("    git stash       # 暂存\n    git stash pop   # 升级后恢复")
        return 1
    if untracked:
        _print(f"忽略 {len(untracked)} 个 untracked 文件（pull 不会动它们）", "info")

    # 2. 升级前 HEAD
    before = _run(["git", "rev-parse", "HEAD"]).strip()

    # 3. git pull
    _print(f"拉取 origin/{args.branch}", "step")
    _run(["git", "pull", "--ff-only", "origin", args.branch], dry=args.dry_run)

    # 4. 升级后 HEAD
    after = _run(["git", "rev-parse", "HEAD"]).strip() if not args.dry_run else ""

    if not args.dry_run and before == after:
        _print("已经是最新版本，无升级。", "ok")
        return 0

    # 5. base deps
    if not args.skip_deps:
        _print("升级 backend base deps", "step")
        _run([sys.executable, "-m", "pip", "install", "-q", "-r", str(REQ_BASE)],
             dry=args.dry_run, check=False)

        # 6. macOS desktop bridge deps
        if _is_macos() and REQ_DESKTOP.exists():
            _print("检测到 macOS → 升级 Desktop 桥接 deps (pyobjc)", "step")
            _run([sys.executable, "-m", "pip", "install", "-q", "-r", str(REQ_DESKTOP)],
                 dry=args.dry_run, check=False)

    # 7. 提示 / 重启 backend
    if args.restart:
        _restart_backend(dry=args.dry_run)
    else:
        running = _backend_running_pids() if not args.dry_run else []
        if running:
            _print(f"backend 仍在跑 (pids={running})，**需要重启才能生效**：", "warn")
            print("    pkill -f 'uvicorn backend.app' && \\")
            print("      nohup python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8787 \\")
            print("      > /tmp/claude-notify.log 2>&1 &")
            print("  或：python3 scripts/update.py --restart  # 下次让脚本自动重启")
        else:
            _print("backend 当前未跑。启动方式：", "info")
            print("    nohup python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8787 \\")
            print("      > /tmp/claude-notify.log 2>&1 &")

    # 8. 变更摘要
    if before and after and before != after:
        print()
        _print(f"本次升级覆盖的 commits ({before[:7]}..{after[:7]})：", "step")
        _run(["git", "log", "--oneline", f"{before}..{after}"], check=False)
        if (PROJECT_ROOT / "CHANGELOG.md").exists():
            _print("详细变更见 CHANGELOG.md", "info")

    _print("升级完成 🎉", "ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _print("用户中断", "warn")
        sys.exit(130)
