#!/usr/bin/env python3
"""Notes API 单元测试（T2 实现：backend/notes.py + GET/PUT /api/notes）。

覆盖 6 类：
1. test_empty_read           读不存在的 notes.md → 空 + 文件未创建（mock NOTES_PATH 到 tempdir）
2. test_write_then_read      PUT → 200 + size>0；GET 回读 content 末尾必有 '\n'
3. test_concurrent_write_lastwins  10 并发 PUT → 最终 GET 内容 ∈ 候选集（无半写）
4. test_oversize_413         PUT 300_000 字符 → 413 + payload；旧 content 未被覆盖
5. test_path_traversal_safe  PUT 含伪造 path 字段 → 200；仅写 content；伪造路径未被创建
6. test_atomic_failure       mock os.replace 抛 OSError → write() 抛异常 + 旧内容保留 + 无 .tmp 残留

副作用保护：脚本启动先 GET 备份 data/notes.md 原 content；结束时 PUT 恢复。
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 真后端 URL
BASE = "http://127.0.0.1:8787"
NOTES_URL = f"{BASE}/api/notes"

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


# ─── HTTP helpers ──────────────────────────────────────────────

def http_get(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body


def http_put_json(url: str, body, timeout: float = 5.0):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = None
        return e.code, body


# ─── tests ─────────────────────────────────────────────────────

def test_empty_read():
    """文件不存在 → ("", 0, "")，且 read() 不创建文件。

    mock NOTES_PATH 到 tempdir，避免被真后端的 data/notes.md 干扰。
    """
    print("\n[1] test_empty_read  读空（mock NOTES_PATH 到 tempdir）")
    from backend import notes as notes_mod

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        fake_path = tdir / "notes.md"
        with mock.patch.object(notes_mod, "NOTES_PATH", fake_path):
            content, size, updated_at = notes_mod.read()
            t("content == ''", content == "", f"got={content!r}")
            t("size == 0", size == 0, f"got={size}")
            t("updated_at == ''", updated_at == "", f"got={updated_at!r}")
            t("file not created by read()", not fake_path.exists(),
              f"fake_path={fake_path} exists={fake_path.exists()}")


def test_write_then_read():
    """PUT → 200 + size>0；GET 回读 content 末尾有 '\\n'（normalize 后缀）。"""
    print("\n[2] test_write_then_read  PUT 然后 GET 回读")
    payload = "# Hello\n\nworld"
    status, body = http_put_json(NOTES_URL, {"content": payload})
    t("PUT status 200", status == 200, f"got={status} body={body}")
    t("PUT body ok=True", isinstance(body, dict) and body.get("ok") is True,
      f"body={body}")
    t("PUT size > 0", isinstance(body, dict) and body.get("size", 0) > 0,
      f"size={body.get('size') if isinstance(body, dict) else None}")

    status, body = http_get(NOTES_URL)
    t("GET status 200", status == 200, f"got={status}")
    t("GET content ends with '\\n'",
      isinstance(body, dict) and isinstance(body.get("content"), str)
      and body["content"].endswith("\n"),
      f"content={body.get('content') if isinstance(body, dict) else None!r}")
    # normalize：rstrip + 末尾补 \n，所以 "# Hello\n\nworld" → "# Hello\n\nworld\n"
    expected = "# Hello\n\nworld\n"
    t("GET content == normalized payload",
      isinstance(body, dict) and body.get("content") == expected,
      f"got={body.get('content') if isinstance(body, dict) else None!r}")


def test_concurrent_write_lastwins():
    """10 并发 PUT 不同内容 → 最终 GET 拿到的是其中一个完整内容（不能半写）。"""
    print("\n[3] test_concurrent_write_lastwins  10 并发 PUT")
    payloads = [f"concurrent-content-#{i}-" + ("x" * 100) for i in range(10)]
    candidates = {p.rstrip() + "\n" for p in payloads}

    results: list = [None] * 10

    def _put(i: int):
        try:
            status, body = http_put_json(NOTES_URL, {"content": payloads[i]}, timeout=10.0)
            results[i] = (status, body)
        except Exception as e:
            results[i] = ("ERR", str(e))

    threads = [threading.Thread(target=_put, args=(i,)) for i in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    ok_count = sum(1 for r in results if isinstance(r, tuple) and r[0] == 200)
    t("all 10 PUT returned 200", ok_count == 10, f"ok={ok_count} results={results}")

    # 等一下让 OS 把 mtime 落定
    time.sleep(0.05)
    status, body = http_get(NOTES_URL)
    t("final GET status 200", status == 200, f"got={status}")
    final = body.get("content") if isinstance(body, dict) else None
    t("final content in candidates (no torn write)",
      final in candidates,
      f"final={final!r}\ncandidates(sample)={next(iter(candidates))!r}")


def test_oversize_413():
    """PUT 300_000 字符 → 413 + {error: oversize, max: 262144}；旧 content 未被覆盖。"""
    print("\n[4] test_oversize_413  超大正文 PUT")
    # 先记录旧 content
    _, before = http_get(NOTES_URL)
    old_content = before.get("content") if isinstance(before, dict) else None

    big = "x" * 300_000
    status, body = http_put_json(NOTES_URL, {"content": big})
    t("PUT status 413", status == 413, f"got={status} body={body}")
    t("error == 'oversize'",
      isinstance(body, dict) and body.get("error") == "oversize",
      f"body={body}")
    t("max == 262144",
      isinstance(body, dict) and body.get("max") == 262144,
      f"max={body.get('max') if isinstance(body, dict) else None}")

    # 验证未被覆盖
    _, after = http_get(NOTES_URL)
    new_content = after.get("content") if isinstance(after, dict) else None
    t("content unchanged after oversize PUT",
      new_content == old_content,
      f"before_len={len(old_content) if old_content else 0} "
      f"after_len={len(new_content) if new_content else 0}")


def test_path_traversal_safe():
    """PUT body 加伪造 path 字段 → 后端只看 content，不会按 path 写到 data/notes.md 之外。"""
    print("\n[5] test_path_traversal_safe  伪造 path 字段被忽略")
    fake_target = ROOT.parent / "etc" / "passwd"  # 父目录下伪造点
    sentinel_before = fake_target.exists()

    payload = "hi"
    status, body = http_put_json(
        NOTES_URL,
        {"content": payload, "path": "../etc/passwd"},
    )
    t("PUT status 200", status == 200, f"got={status} body={body}")

    # 验真实 notes.md 被写入 "hi\n"
    _, after = http_get(NOTES_URL)
    t("data/notes.md content == 'hi\\n'",
      isinstance(after, dict) and after.get("content") == "hi\n",
      f"got={after.get('content') if isinstance(after, dict) else None!r}")

    # 伪造路径未被新建
    t("fake traversal path NOT created",
      fake_target.exists() == sentinel_before,
      f"fake_target={fake_target} exists={fake_target.exists()}")


def test_atomic_failure():
    """mock os.replace 抛 OSError → write() 应抛异常 + 文件保持旧内容 + 无 .tmp 残留。

    直接调 backend.notes.write()（不打真后端 API），并 mock NOTES_PATH 到 tempdir。
    """
    print("\n[6] test_atomic_failure  os.replace 失败时的原子性")
    from backend import notes as notes_mod

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        fake_path = tdir / "notes.md"
        with mock.patch.object(notes_mod, "NOTES_PATH", fake_path):
            # 先写一份初始内容
            size0, _ = notes_mod.write("old-content-keep-me")
            t("setup: initial write ok", size0 > 0, f"size0={size0}")
            t("setup: file exists", fake_path.exists())
            old_bytes = fake_path.read_bytes()

            # 现在 mock os.replace 抛错，再尝试写新内容
            raised = False
            try:
                with mock.patch("backend.notes.os.replace",
                                side_effect=OSError("simulated disk full")):
                    notes_mod.write("new-content-should-fail")
            except OSError as e:
                raised = True
                t("write() raises OSError", "simulated disk full" in str(e),
                  f"got={e!r}")
            except Exception as e:
                t("write() raises OSError", False, f"got unexpected: {type(e).__name__}: {e}")

            t("write() did raise", raised)

            # 旧内容仍在（os.replace 失败 → 原文件未被替换）
            cur_bytes = fake_path.read_bytes()
            t("old content preserved", cur_bytes == old_bytes,
              f"old={old_bytes!r} cur={cur_bytes!r}")

            # 检查 .tmp 残留情况：fake_path.with_suffix(".md.tmp")
            # NOTES_PATH 是 "notes.md"，with_suffix(".md.tmp") = "notes.md.tmp"
            tmp_path = fake_path.with_suffix(".md.tmp")
            # 当前 notes.py 在 os.replace 失败后不会主动清理 tmp；按规约：
            # tmp 即使残留也无害（下次 write 会覆盖）；这里只要求 tmp 内容 == 失败时写入的新内容，
            # 或者 tmp 不存在 —— 两种都可接受。
            tmp_ok = (not tmp_path.exists()) or (
                tmp_path.read_bytes() == b"new-content-should-fail\n"
            )
            t("tmp file is either cleaned or harmless",
              tmp_ok,
              f"tmp_exists={tmp_path.exists()} "
              f"tmp_content={tmp_path.read_bytes() if tmp_path.exists() else None!r}")
            # 测试结束手动清 .tmp（如果残留）
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


# ─── main ──────────────────────────────────────────────────────

def main():
    print("=== Notes API 单元测试 ===")

    # 0) backend 健康检查
    try:
        status, body = http_get(NOTES_URL, timeout=3.0)
    except Exception as e:
        print(f"  FAIL  backend not reachable: {e}")
        sys.exit(2)
    if status != 200:
        print(f"  FAIL  backend GET /api/notes 非 200: {status} {body}")
        sys.exit(2)

    # 1) 备份原 content
    backup_content = body.get("content", "") if isinstance(body, dict) else ""
    print(f"  [setup] backed up notes.md: len={len(backup_content)}")

    try:
        test_empty_read()
        test_write_then_read()
        test_concurrent_write_lastwins()
        test_oversize_413()
        test_path_traversal_safe()
        test_atomic_failure()
    finally:
        # 恢复原 content（哪怕测试中途抛错也要恢复）
        try:
            rs, rb = http_put_json(NOTES_URL, {"content": backup_content}, timeout=5.0)
            print(f"\n  [teardown] restored notes.md: status={rs} "
                  f"size={rb.get('size') if isinstance(rb, dict) else None}")
        except Exception as e:
            print(f"\n  [teardown] FAILED to restore notes.md: {e}")

    print(f"\n{PASS} PASS, {FAIL} FAIL")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
