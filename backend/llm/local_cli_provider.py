"""LocalCLIProvider —— 调本机 `claude -p` 走官方 Claude Code CLI。

双模：
- 有 ANTHROPIC_API_KEY → 加 `--bare` 跳过 hooks/plugin/CLAUDE.md，~0.7s/次
- 无 key → 默认模式走 OAuth（Claude Max 订阅），11-15s/次

目的：
- Claude Code 用户开箱即用（不强制配 API key）
- 与 ctf/reclaude 渗透壳并存（绝对路径调用，不污染 PATH）

不依赖 anthropic SDK；用 asyncio.subprocess 拉子进程。
"""
from __future__ import annotations
import asyncio
import logging
import os
import shutil
from typing import Any

from .base import LLMResult

log = logging.getLogger("claude-notify.llm.local_cli")

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TIMEOUT_S = 30.0  # OAuth 冷启动 11-15s，留余量


def _discover_claude_bin() -> str:
    """优先 env / 用户配置；其次 PATH 上的 claude；最后常见 nvm 路径。"""
    p = (os.environ.get("CLAUDE_NOTIFY_CLAUDE_BIN") or "").strip()
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    found = shutil.which("claude")
    if found:
        return found
    # nvm fallback
    home = os.path.expanduser("~")
    nvm_root = os.path.join(home, ".nvm/versions/node")
    if os.path.isdir(nvm_root):
        for v in sorted(os.listdir(nvm_root), reverse=True):
            cand = os.path.join(nvm_root, v, "bin/claude")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return ""


class LocalCLIProvider:
    name = "local_cli"

    def __init__(
        self,
        binary: str,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        force_mode: str = "auto",   # auto | bare | oauth
        extra_args: list[str] | None = None,
    ):
        self.binary = binary
        self.model = model
        self.timeout_s = timeout_s
        self.force_mode = force_mode
        self.extra_args = extra_args or []

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "LocalCLIProvider":
        binary = (cfg.get("binary") or "").strip() or _discover_claude_bin()
        if not binary:
            raise RuntimeError("claude binary not found (set llm.local_cli.binary or PATH)")
        model = (cfg.get("model") or "").strip() or DEFAULT_MODEL
        timeout_s = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        force_mode = (cfg.get("mode") or "auto").strip().lower()
        if force_mode not in {"auto", "bare", "oauth"}:
            force_mode = "auto"
        extra = cfg.get("extra_args") or []
        if isinstance(extra, str):
            extra = extra.split()
        return cls(binary=binary, model=model, timeout_s=timeout_s,
                   force_mode=force_mode, extra_args=list(extra))

    def _resolve_mode(self) -> str:
        """auto: 有 ANTHROPIC_API_KEY → bare（快），否则 oauth（慢但免 key）。"""
        if self.force_mode in ("bare", "oauth"):
            return self.force_mode
        if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
            return "bare"
        return "oauth"

    async def complete(self, system: str, user: str, max_tokens: int = 200) -> LLMResult:
        mode = self._resolve_mode()

        # token 优化（教训 L04 + L06）：
        # - bare 模式：跳 CLAUDE.md / auto-memory / hooks（强制 API key），用 --system-prompt 替换。
        # - oauth 模式：以上文件无法跳（Claude Code OAuth 流程依赖），目标改为「最大化 prompt cache
        #   命中」。fresh input tokens 远 << cache read tokens，后者几乎不计费。
        #   策略：默认 system prompt + --append-system-prompt 追加摘要指令；不传
        #   --exclude-dynamic-system-prompt-sections（实测它把 dynamic sections 移到 user msg
        #   反而每次 6-8K cache_creation，因为 user msg 每次内容不同 cache 不复用；留在 system
        #   prompt 里则 cwd=/tmp 固定 → cache 完全 stable hit）。
        argv = [self.binary, "-p", "--model", self.model]
        if mode == "bare":
            argv.append("--bare")
            if system:
                argv.extend(["--system-prompt", system])
        else:  # oauth（含 auto fallback）
            if system:
                argv.extend(["--append-system-prompt", system])
        argv.extend(self.extra_args)

        # 子进程标记：反馈循环防护（L02）。reclaude/claude/ctf 子进程会触发 ~/.claude/settings.json
        # 全局 hook → 写 events.jsonl → backend 误识别成新 session → 又调 LLM → 无限递归。
        # hook-notify.py 见到此 env 立即退出 0，不写 events。
        env = os.environ.copy()
        env["CLAUDE_NOTIFY_LLM_CHILD"] = "1"

        # cwd=/tmp：跳过项目级 CLAUDE.md auto-discovery + cwd 稳定 → 跨调用 cache 复用更高。
        # 用户全局 ~/.claude/CLAUDE.md 仍读（OAuth 鉴权依赖），但每次都一样 → 稳定 cache hit。
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd="/tmp",
            )
        except FileNotFoundError as e:
            return LLMResult(text="", ok=False, reason=f"binary_not_found: {e}",
                             model=self.model)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(user.encode("utf-8")),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return LLMResult(text="", ok=False,
                             reason=f"timeout_{int(self.timeout_s)}s_mode={mode}",
                             model=self.model)

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:200]
            return LLMResult(text="", ok=False,
                             reason=f"rc={proc.returncode} mode={mode}: {err}",
                             model=self.model)

        text = (stdout or b"").decode("utf-8", errors="replace").strip()
        if not text:
            err = (stderr or b"").decode("utf-8", errors="replace")[:120]
            return LLMResult(text="", ok=False,
                             reason=f"empty_output mode={mode} stderr={err}",
                             model=self.model)
        # claude -p 默认输出纯文本，不需要 JSON 解析
        return LLMResult(
            text=text,
            ok=True,
            input_tokens=0,
            output_tokens=0,
            model=f"{self.model}({mode})",
        )
