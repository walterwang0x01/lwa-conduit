"""单元测试：semantic.py 的 parse + reviewer 实现。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_conduit.semantic import (
    NoOpSemanticReviewer,
    ReviewContext,
    ReviewResult,
    SemanticReviewer,
    parse_review_response,
    run_with_timeout,
)


def make_ctx(tmp_path: Path) -> ReviewContext:
    return ReviewContext(
        task_id="t1",
        task_prompt="implement add",
        diff="+def add(a, b): return a+b\n",
        cwd=tmp_path,
    )


# ---------------------------------------------------------------------------
# parse_review_response
# ---------------------------------------------------------------------------


class TestParseReviewResponse:
    def test_pass_with_reason(self) -> None:
        passed, fb = parse_review_response("PASS\nlooks good")
        assert passed
        assert fb == "looks good"

    def test_fail_with_reason(self) -> None:
        passed, fb = parse_review_response("FAIL\nfunc f returns wrong type")
        assert not passed
        assert "wrong type" in fb

    def test_pass_only(self) -> None:
        passed, fb = parse_review_response("PASS")
        assert passed
        assert "passed without comment" in fb

    def test_fail_only(self) -> None:
        passed, fb = parse_review_response("FAIL")
        assert not passed
        assert "failed without explanation" in fb

    def test_lowercase_treated_as_uppercase(self) -> None:
        passed, fb = parse_review_response("  fail \n  reason: bad")
        assert not passed
        assert "reason: bad" in fb

    def test_keyword_in_middle_treated_as_verdict(self) -> None:
        # 第一行没明确判定，但全文有 FAIL → 当 FAIL
        passed, _ = parse_review_response("Looking at this... It seems FAIL because...")
        assert not passed

    def test_empty_response_pass_default(self) -> None:
        passed, fb = parse_review_response("")
        assert passed
        assert "empty" in fb.lower() or "default" in fb.lower()

    def test_no_keyword_at_all_pass_default(self) -> None:
        passed, fb = parse_review_response("just rambling without verdict")
        assert passed
        # 原文应该被保留
        assert "rambling" in fb


# ---------------------------------------------------------------------------
# NoOpSemanticReviewer
# ---------------------------------------------------------------------------


class TestNoOpSemanticReviewer:
    @pytest.mark.asyncio
    async def test_always_passes(self, tmp_path: Path) -> None:
        result = await NoOpSemanticReviewer().review(make_ctx(tmp_path))
        assert result.passed
        assert "no-op" in result.feedback


# ---------------------------------------------------------------------------
# Protocol 兼容性
# ---------------------------------------------------------------------------


class TestProtocolCompat:
    def test_noop_satisfies_protocol(self) -> None:
        # 显式断言 NoOp 是 SemanticReviewer 的实现
        reviewer: SemanticReviewer = NoOpSemanticReviewer()
        # 静态可达就够；运行期 Protocol 默认不严格检查
        assert reviewer is not None


# ---------------------------------------------------------------------------
# run_with_timeout
# ---------------------------------------------------------------------------


class _SlowReviewer:
    """sleeps for 1s, then PASS."""

    async def review(self, ctx: ReviewContext) -> ReviewResult:
        await asyncio.sleep(1.0)
        return ReviewResult(passed=True, feedback="slow but ok")


class _RaisingReviewer:
    async def review(self, ctx: ReviewContext) -> ReviewResult:
        raise RuntimeError("boom")


class _FailingReviewer:
    async def review(self, ctx: ReviewContext) -> ReviewResult:
        return ReviewResult(passed=False, feedback="nope")


class TestRunWithTimeout:
    @pytest.mark.asyncio
    async def test_normal_completion(self, tmp_path: Path) -> None:
        result = await run_with_timeout(
            NoOpSemanticReviewer(), make_ctx(tmp_path), timeout=5.0
        )
        assert result.passed

    @pytest.mark.asyncio
    async def test_timeout_fails_open(self, tmp_path: Path) -> None:
        result = await run_with_timeout(
            _SlowReviewer(), make_ctx(tmp_path), timeout=0.1
        )
        assert result.passed  # fail-open
        assert "timed out" in result.feedback

    @pytest.mark.asyncio
    async def test_failing_reviewer_passed_through(self, tmp_path: Path) -> None:
        """reviewer 自己说 fail，run_with_timeout 不应该改成 PASS。"""
        result = await run_with_timeout(
            _FailingReviewer(), make_ctx(tmp_path), timeout=5.0
        )
        assert not result.passed
        assert result.feedback == "nope"

    @pytest.mark.asyncio
    async def test_reviewer_exception_propagates(self, tmp_path: Path) -> None:
        """run_with_timeout 只兜超时；其他异常该抛就抛——上层（Verifier 或 KiroReviewer
        自己）决定是否做 fail-open 包装。"""
        with pytest.raises(RuntimeError, match="boom"):
            await run_with_timeout(
                _RaisingReviewer(), make_ctx(tmp_path), timeout=5.0
            )


class TestReviewIntegration:
    """review_integration：对整条集成 diff 对照 specs 做一次审。"""

    @pytest.mark.asyncio
    async def test_reviews_integration_diff_against_specs(self, tmp_path: Path) -> None:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*a: str) -> None:
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "t")
        (repo / "f.py").write_text("x = 1\n")
        git("add", ".")
        git("commit", "-m", "init")
        # 集成分支加一处改动
        git("checkout", "-b", "kiro-conduit/integration")
        (repo / "f.py").write_text("x = 1\ny = 2\n")
        git("add", ".")
        git("commit", "-m", "feat")
        git("checkout", "main")

        specs = tmp_path / "specs"
        specs.mkdir()
        (specs / "a.md").write_text("加一个 y")

        seen: dict[str, str] = {}

        class _Fake:
            async def review(self, ctx: ReviewContext) -> ReviewResult:
                seen["diff"] = ctx.diff
                seen["spec"] = ctx.task_prompt
                return ReviewResult(passed=False, feedback="发现：缺少类型注解")

        from kiro_conduit.semantic import review_integration

        result = await review_integration(
            base_repo=repo, base_branch="main",
            integration_ref="kiro-conduit/integration",
            specs_dir=specs, reviewer=_Fake(),
        )
        assert result.passed is False
        assert "缺少类型注解" in result.feedback
        assert "y = 2" in seen["diff"]   # 真的把集成 diff 喂给了 reviewer
        assert "加一个 y" in seen["spec"]  # 真的把 specs 喂进去了

    @pytest.mark.asyncio
    async def test_empty_diff_passes_without_calling_reviewer(self, tmp_path: Path) -> None:
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()

        def git(*a: str) -> None:
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t.com")
        git("config", "user.name", "t")
        (repo / "f.py").write_text("x = 1\n")
        git("add", ".")
        git("commit", "-m", "init")

        called = {"n": 0}

        class _Fake:
            async def review(self, ctx: ReviewContext) -> ReviewResult:
                called["n"] += 1
                return ReviewResult(passed=False, feedback="should not run")

        from kiro_conduit.semantic import review_integration

        result = await review_integration(
            base_repo=repo, base_branch="main", integration_ref="main",
            specs_dir=tmp_path / "nope", reviewer=_Fake(),
        )
        assert result.passed is True  # 空 diff → 直接 PASS
        assert called["n"] == 0  # 没必要调 reviewer
