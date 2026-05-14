#!/usr/bin/env python3
"""Round 9 (L27) 测试：/api/health/setup endpoint schema + 首次接入引导触发逻辑。

不动用户实际 settings.json 与 config.json：
  - schema 测试：调实际 endpoint，验证返回字段齐全 + 类型正确
  - 触发逻辑：确认 0 session + 无 webhook + 无 hook 时 3 项都 false
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from unittest import mock

URL = "http://127.0.0.1:8787/api/health/setup"
PASS = 0
FAIL = 0


def t(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_endpoint_schema():
    print("\n[schema] /api/health/setup 字段齐全 + 类型正确")
    try:
        with urllib.request.urlopen(URL, timeout=5) as r:
            data = json.loads(r.read())
    except Exception as e:
        t("endpoint reachable", False, str(e))
        return
    t("endpoint reachable", True)
    required = [
        "hooks_registered", "hooks_detected", "hooks_expected",
        "settings_exists", "webhook_configured", "session_count",
    ]
    for k in required:
        t(f"key '{k}' present", k in data)
    if all(k in data for k in required):
        t("hooks_registered is bool", isinstance(data["hooks_registered"], bool))
        t("webhook_configured is bool", isinstance(data["webhook_configured"], bool))
        t("session_count is int", isinstance(data["session_count"], int))
        t("hooks_detected is list", isinstance(data["hooks_detected"], list))
        t("hooks_expected non-empty", len(data["hooks_expected"]) >= 4)
        t("Notification in expected", "Notification" in data["hooks_expected"])


def test_detection_logic_with_mock():
    """直接 import backend.app，用 monkeypatch 验证检测逻辑。"""
    print("\n[unit] 检测逻辑分支（不动用户实际 settings.json）")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend import app as app_mod
    from backend import config as cfg_mod
    from backend import event_store

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        # 准备一个无 hook 的 settings.json
        empty_settings = tdir / "empty_settings.json"
        empty_settings.write_text("{}", "utf-8")
        # 准备一个 4 hook 全装的 settings.json
        full_settings = tdir / "full_settings.json"
        full_settings.write_text(json.dumps({
            "hooks": {
                ev: [{"hooks": [{"type": "command", "command": f"python3 /tmp/hook-notify.py"}]}]
                for ev in ["Notification", "Stop", "SubagentStop", "PreToolUse"]
            }
        }), "utf-8")

        # 场景 A：settings 不存在 + webhook 空
        with mock.patch.object(Path, "home", return_value=tdir / "no_such_dir"):
            with mock.patch.object(cfg_mod, "load", return_value={"feishu_webhook": ""}):
                with mock.patch.object(event_store, "list_sessions", return_value=[]):
                    r = app_mod.health_setup()
        t("A: settings_exists=False", r["settings_exists"] is False)
        t("A: hooks_registered=False", r["hooks_registered"] is False)
        t("A: webhook_configured=False", r["webhook_configured"] is False)
        t("A: session_count=0", r["session_count"] == 0)
        t("A: hooks_detected=[]", r["hooks_detected"] == [])

        # 场景 B：4 hook 全装 + webhook 配好 + 3 sessions
        # mock Path.home 返回包含 .claude/settings.json 的目录
        fake_home = tdir / "fake_home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text(full_settings.read_text("utf-8"), "utf-8")
        with mock.patch.object(Path, "home", return_value=fake_home):
            with mock.patch.object(cfg_mod, "load", return_value={"feishu_webhook": "https://open.feishu.cn/x"}):
                with mock.patch.object(event_store, "list_sessions",
                                       return_value=[{"session_id": f"s{i}"} for i in range(3)]):
                    r = app_mod.health_setup()
        t("B: settings_exists=True", r["settings_exists"] is True)
        t("B: hooks_registered=True", r["hooks_registered"] is True)
        t("B: hooks_detected=4", len(r["hooks_detected"]) == 4)
        t("B: webhook_configured=True", r["webhook_configured"] is True)
        t("B: session_count=3", r["session_count"] == 3)

        # 场景 C：仅 2 hook 装上（< 3 → 视为未装好）
        partial = tdir / "fake_home_partial"
        (partial / ".claude").mkdir(parents=True)
        (partial / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {
                ev: [{"hooks": [{"type": "command", "command": "python3 /tmp/hook-notify.py"}]}]
                for ev in ["Notification", "Stop"]
            }
        }), "utf-8")
        with mock.patch.object(Path, "home", return_value=partial):
            with mock.patch.object(cfg_mod, "load", return_value={"feishu_webhook": "x"}):
                with mock.patch.object(event_store, "list_sessions", return_value=[]):
                    r = app_mod.health_setup()
        t("C: 2 hooks → registered=False", r["hooks_registered"] is False)
        t("C: 2 hooks → detected=2", len(r["hooks_detected"]) == 2)


def main():
    print(f"=== L27 setup health endpoint test ===")
    test_endpoint_schema()
    test_detection_logic_with_mock()
    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
