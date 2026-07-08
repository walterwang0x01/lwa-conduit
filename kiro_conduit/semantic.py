"""Verifier Layer 3：AI 语义评审。

可插拔的后端：
- NoOpSemanticReviewer：默认，直接 PASS。CI / 无 Kiro 环境 / 想省 token 时用
- KiroSemanticReviewer：spawn 独立的 kiro-cli acp 子进程跑 review prompt

设计要点：
- Reviewer 跟 Implementor 是独立 ACP session（CIV 模式：审查者不能跟执行者共享上下文）
- review prompt 要求 LLM 输出 PASS / FAIL + 简短理由（结构化，便于后续机器解析）
- 解析很简单：找第一行 PASS / FAIL，剩下的当 feedback
- 失败要短路：earlier layer 挂了不跑这层（Verifier 自己控制顺序）
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kiro_conduit.runtime.cursor_cli import cursor_prompt_text
from kiro_conduit.runtime.model_router import resolve_runtime_for_prompt
from kiro_conduit.runtime.types import RuntimeConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewContext:
    """传给 reviewer 的上下文。"""

    task_id: str
    task_prompt: str  # Implementor 当时收到的指令
    diff: str         # Implementor 产出的 git diff
    cwd: Path         # worktree 路径，reviewer 可以读真实文件


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """reviewer 返回的结论。"""

    passed: bool
    feedback: str  # 给 Coordinator 重试时塞进 prompt 的反馈

    @classmethod
    def passed_default(cls) -> ReviewResult:
        return cls(passed=True, feedback="(no semantic review configured)")


class SemanticReviewer(Protocol):
    """可插拔的语义 reviewer。"""

    async def review(self, ctx: ReviewContext) -> ReviewResult: ...


# ---------------------------------------------------------------------------
# 默认实现：直接 PASS
# ---------------------------------------------------------------------------


class NoOpSemanticReviewer:
    """什么都不做的 reviewer，直接 PASS。

    用途：
    - 单元测试默认（不依赖任何外部进程）
    - 用户没显式配 reviewer 时的兜底
    - CI 环境（不希望跑真 LLM）
    """

    async def review(self, ctx: ReviewContext) -> ReviewResult:
        return ReviewResult(
            passed=True,
            feedback=f"(no-op semantic review for {ctx.task_id})",
        )


# ---------------------------------------------------------------------------
# Kiro 实现：spawn 独立 ACP 进程跑 review
# ---------------------------------------------------------------------------


REVIEW_PROMPT_TEMPLATE = """你是一个独立的代码评审 agent。下面是另一个 agent
（"Implementor"）刚刚在工作目录里完成的一个任务。请检查它的产出**语义上**是否
满足任务要求——超越 lint / 单元测试已经覆盖的范围。

## 评判标准

PASS 的条件（满足全部）：
- 实现确实完成了任务说明里的核心目标
- 没有明显的设计错误（如：用错数据结构、忽略边界情况、偷偷绕过指令）
- 代码风格和已有项目一致

FAIL 的条件（任一）：
- 任务的核心要求没做到（比如要求异步实现却写了同步）
- 有明显逻辑 bug（lint / pytest 没抓到的那种）
- 越界改了不该改的文件 / 接口
- 偷工减料（比如往函数里写 `raise NotImplementedError` 假装做了）

## 输出格式（必须严格遵守）

第一行：`PASS` 或 `FAIL`（仅这两个词之一，全大写）
之后的内容：简短理由（1-3 句话），如果 FAIL 要说清楚问题在哪、怎么改。

不要输出别的东西——不要 markdown 代码块、不要 emoji、不要长篇分析。

## 任务上下文

**Task ID**: {task_id}

**Implementor 当时收到的指令**：

{task_prompt}

**Implementor 的 git diff**（可能很长）：

```
{diff}
```

请基于上面信息给出 PASS / FAIL 评审。"""


class KiroSemanticReviewer:
    """用独立的 kiro-cli acp 子进程跑评审 prompt。"""

    def __init__(
        self,
        runtime: RuntimeConfig | None = None,
        *,
        kiro_cli_path: str = "kiro-cli",
        timeout: float = 180.0,
        max_diff_chars: int = 30000,
        model: str | None = None,
    ) -> None:
        self._runtime = runtime or RuntimeConfig.from_cli(
            kiro_cli=kiro_cli_path,
            runtime_kind="kiro-cli-acp",
            model=model,
            timeout=timeout,
        )
        self._timeout = timeout
        self._max_diff_chars = max_diff_chars
        self._model = model

    async def review(self, ctx: ReviewContext) -> ReviewResult:
        # 截短 diff 防止 prompt 爆炸（M1.1 简单粗暴；M1.2 可改成 chunk 分批）
        diff = ctx.diff
        if len(diff) > self._max_diff_chars:
            diff = (
                diff[: self._max_diff_chars]
                + f"\n\n... [diff truncated at {self._max_diff_chars} chars]"
            )

        prompt = REVIEW_PROMPT_TEMPLATE.format(
            task_id=ctx.task_id,
            task_prompt=ctx.task_prompt,
            diff=diff,
        )

        try:
            response = await self._run_kiro_review(ctx.cwd, prompt)
        except (TimeoutError, ConnectionError) as exc:
            # reviewer 本身挂了不应该让 verifier 也挂——按 fail-open 处理：log 一下，PASS
            # 这是 ARCHITECTURE.md "verifier 挂了不能阻塞业务" 的简化版
            logger.warning(
                "[semantic-review] reviewer crashed for %s: %s; failing open (PASS)",
                ctx.task_id,
                exc,
            )
            return ReviewResult(
                passed=True,
                feedback=f"(reviewer crashed: {exc}; failed open)",
            )

        passed, feedback = parse_review_response(response)
        return ReviewResult(passed=passed, feedback=feedback)

    async def _run_kiro_review(self, cwd: Path, prompt: str) -> str:
        """起一个独立 ACP session，发 review prompt，收齐所有 message chunk 拼成响应。"""
        runtime = resolve_runtime_for_prompt(self._runtime, prompt)
        if runtime.kind == "cursor-agent-cli":
            return await cursor_prompt_text(runtime, cwd=cwd, prompt=prompt)

        # 局部 import 避免顶部循环依赖
        from kiro_conduit.acp import (
            AcpClient,
            AcpClientConfig,
            AgentMessageChunk,
            TurnEnd,
        )

        config = AcpClientConfig(
            kiro_cli_path=runtime.bin,
            cwd=cwd,
            response_timeout=self._timeout,
            model=self._model or runtime.model,
        )
        chunks: list[str] = []
        async with await AcpClient.spawn(config) as client:
            await client.initialize()
            session_id = await client.new_session(cwd=cwd)
            events = await client.prompt(session_id, prompt)
            async for event in events:
                if isinstance(event, AgentMessageChunk):
                    chunks.append(event.text)
                elif isinstance(event, TurnEnd):
                    break
        return "".join(chunks).strip()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


_VERDICT_RE = re.compile(r"\b(PASS|FAIL)\b", re.IGNORECASE)


def parse_review_response(response: str) -> tuple[bool, str]:
    """把 LLM 的回复解析成 (passed, feedback)。

    宽容处理：
    - 第一行有 PASS 就 PASS
    - 第一行有 FAIL 就 FAIL
    - 第一行都没有，扫整段：含 FAIL 当 FAIL（偏严，避免假阳性 PASS）
    - 完全找不到判定关键词，按 PASS 处理（fail-open）但保留原文当 feedback
    """
    if not response.strip():
        return True, "(empty review response, defaulting to PASS)"

    lines = response.strip().splitlines()
    first = lines[0].strip().upper() if lines else ""
    rest = "\n".join(lines[1:]).strip()

    if first == "PASS" or first.startswith("PASS"):
        return True, rest or "(passed without comment)"
    if first == "FAIL" or first.startswith("FAIL"):
        return False, rest or "(failed without explanation)"

    # 第一行没明确判定：扫全文
    match = _VERDICT_RE.search(response)
    if match is None:
        return True, f"(no PASS/FAIL keyword found, defaulting to PASS)\n{response}"
    verdict = match.group(1).upper()
    return verdict == "PASS", response.strip()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def run_with_timeout(
    reviewer: SemanticReviewer,
    ctx: ReviewContext,
    timeout: float,
) -> ReviewResult:
    """带超时调一次 reviewer。超时算 fail-open（PASS）。"""
    try:
        return await asyncio.wait_for(reviewer.review(ctx), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "[semantic-review] timed out after %.1fs for %s; failing open (PASS)",
            timeout,
            ctx.task_id,
        )
        return ReviewResult(
            passed=True,
            feedback=f"(reviewer timed out after {timeout}s; failed open)",
        )


async def review_integration(
    *,
    base_repo: Path,
    base_branch: str,
    integration_ref: str,
    specs_dir: Path,
    reviewer: SemanticReviewer,
    timeout: float = 600.0,
) -> ReviewResult:
    """对整条集成 diff（base_branch...integration_ref）对照 specs 做一次 AI 初审。

    复用 per-task 的 SemanticReviewer：task_prompt 塞拼接后的 specs（= 拆开的 spec），
    diff 塞整条集成 diff。返回结构化结论，供人只在一份报告上做终审。
    """
    from kiro_conduit.git_utils import run_git

    code, diff, err = await run_git(
        base_repo, ["diff", f"{base_branch}...{integration_ref}"]
    )
    if code != 0:
        return ReviewResult(passed=True, feedback=f"(could not diff integration: {err.strip()})")
    if not diff.strip():
        return ReviewResult(passed=True, feedback="(integration diff is empty)")

    spec_text = ""
    if specs_dir.is_dir():
        spec_text = "\n\n".join(
            p.read_text(encoding="utf-8") for p in sorted(specs_dir.glob("*.md"))
        )
    ctx = ReviewContext(
        task_id="integration",
        task_prompt=spec_text or "(no specs found)",
        diff=diff,
        cwd=base_repo,
    )
    return await run_with_timeout(reviewer, ctx, timeout=timeout)
