"""L17 测试：config.save() 反向 diff，避免老用户被 DEFAULTS 旧值 cover。

场景：
1. 用户改 webhook → 只持久化 webhook，不持久化全量 DEFAULTS
2. 用户改 notify_filter.stop_min_summary_chars=20 → 只持久化这一个字段
3. 用户调回 default 值 → 自动从 config.json 移除该字段
4. 老 config.json 含旧 default → 下次 save 自动清理
5. patch 含 DEFAULTS 没有的新字段 → 保留
6. DEFAULTS 改了 → 老用户的 config.json（无该字段）自动跟上
"""
import json
import tempfile
from pathlib import Path
import sys


def setup_isolated_config_module():
    """每个 test 用全新 tempdir 隔离 CONFIG_PATH。"""
    import importlib
    # 先 reset 已 import 的 backend.config
    for mod in list(sys.modules):
        if mod.startswith("backend"):
            del sys.modules[mod]
    tmp = Path(tempfile.mkdtemp(prefix="cn-test-cfg-"))
    # 通过环境变量 / monkey-patch 让 PROJECT_ROOT 指向 tmp 不可行（PROJECT_ROOT 来自 __file__），
    # 改为直接 import 后覆盖路径常量
    import backend.config as cfg
    cfg.DATA_DIR = tmp
    cfg.CONFIG_PATH = tmp / "config.json"
    return cfg, tmp


def test_only_persists_diff_against_defaults():
    cfg, tmp = setup_isolated_config_module()
    cfg.save({"feishu_webhook": "https://open.feishu.cn/xxx"})
    raw = json.loads(cfg.CONFIG_PATH.read_text("utf-8"))
    assert raw == {"feishu_webhook": "https://open.feishu.cn/xxx"}, f"应只持久化 webhook，实际 {raw}"
    print("  PASS 只持久化用户改的 webhook")


def test_legacy_polluted_config_auto_cleans():
    """老 config.json 含 default 值 → 下一次 save 自动清理"""
    cfg, tmp = setup_isolated_config_module()
    # 模拟老用户被污染的 config.json：含一堆 default 值 + 一个真改过的字段
    polluted = {
        "feishu_webhook": "https://open.feishu.cn/xxx",
        "notify_filter": {
            "notif_suppress_after_stop_min": 3,        # 旧 default（现在 0.5）
            "stop_min_milestone_chars": 12,            # 旧 default（现在 6）
            "stop_min_summary_chars": 15,              # 旧 default（现在 8）
            "filter_sidechain_notifications": True,    # 当前 default == True
            "blacklist_words": list(cfg.DEFAULT_BLACKLIST.__class__()) if hasattr(cfg, 'DEFAULT_BLACKLIST') else cfg.DEFAULTS["notify_filter"]["blacklist_words"],
        },
    }
    cfg.CONFIG_PATH.write_text(json.dumps(polluted), "utf-8")
    # 用户随便改一个字段（只改 webhook 也行）
    cfg.save({"feishu_webhook": "https://open.feishu.cn/yyy"})
    raw = json.loads(cfg.CONFIG_PATH.read_text("utf-8"))
    nf = raw.get("notify_filter", {})
    # 原来 5 个字段中：3/12/15 是旧 default 应保留（因为 ≠ 当前 DEFAULTS）；后两项是当前 default 应剪掉
    assert "notif_suppress_after_stop_min" in nf, "旧 default 3 与当前 default 0.5 不同 → 应保留"
    assert "filter_sidechain_notifications" not in nf, "= 当前 default → 应剪掉"
    assert "blacklist_words" not in nf, "= 当前 default → 应剪掉"
    print(f"  PASS 老配置自动清理：剩余 notify_filter 字段 = {list(nf.keys())}")


def test_user_revert_to_default_strips_field():
    cfg, tmp = setup_isolated_config_module()
    # 用户先改成 20
    cfg.save({"notify_filter": {"stop_min_summary_chars": 20}})
    raw1 = json.loads(cfg.CONFIG_PATH.read_text("utf-8"))
    assert raw1.get("notify_filter", {}).get("stop_min_summary_chars") == 20
    # 用户调回 default 值 8
    cfg.save({"notify_filter": {"stop_min_summary_chars": 8}})
    raw2 = json.loads(cfg.CONFIG_PATH.read_text("utf-8"))
    nf = raw2.get("notify_filter", {})
    assert "stop_min_summary_chars" not in nf, f"调回 default 应剪掉，实际 {nf}"
    print("  PASS 用户调回 default 值自动从 config.json 移除")


def test_unknown_user_field_preserved():
    cfg, tmp = setup_isolated_config_module()
    cfg.save({"my_custom_field": {"foo": "bar"}})
    raw = json.loads(cfg.CONFIG_PATH.read_text("utf-8"))
    assert raw.get("my_custom_field") == {"foo": "bar"}, "DEFAULTS 没有的用户字段应保留"
    print("  PASS DEFAULTS 没有的自定义字段保留")


def test_full_view_unchanged():
    cfg, tmp = setup_isolated_config_module()
    cfg.save({"feishu_webhook": "https://open.feishu.cn/xxx"})
    full = cfg.load()
    # full view 含所有 DEFAULTS 字段，前端能正常渲染
    assert full["feishu_webhook"] == "https://open.feishu.cn/xxx"
    assert "notify_filter" in full and "stop_min_summary_chars" in full["notify_filter"]
    assert full["notify_filter"]["stop_min_summary_chars"] == 8
    print("  PASS load() 仍返回 full view（前端渲染不受影响）")


def test_defaults_change_auto_propagates():
    """模拟未来 DEFAULTS 改动，老用户的"未保存字段"自动跟上"""
    cfg, tmp = setup_isolated_config_module()
    cfg.save({"feishu_webhook": "x"})
    # 模拟下一版 DEFAULTS 改了 stop_min_summary_chars 8 → 4
    original = cfg.DEFAULTS["notify_filter"]["stop_min_summary_chars"]
    cfg.DEFAULTS["notify_filter"]["stop_min_summary_chars"] = 4
    try:
        full = cfg.load()
        assert full["notify_filter"]["stop_min_summary_chars"] == 4, "DEFAULTS 改后老用户应自动用新值"
        print(f"  PASS DEFAULTS {original} → 4 老用户自动跟上")
    finally:
        cfg.DEFAULTS["notify_filter"]["stop_min_summary_chars"] = original


def main():
    print("L17 config 反向 diff 验证")
    print("=" * 60)
    test_only_persists_diff_against_defaults()
    test_legacy_polluted_config_auto_cleans()
    test_user_revert_to_default_strips_field()
    test_unknown_user_field_preserved()
    test_full_view_unchanged()
    test_defaults_change_auto_propagates()
    print("=" * 60)
    print("ALL PASS")


if __name__ == "__main__":
    main()
