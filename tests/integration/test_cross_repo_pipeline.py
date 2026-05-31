"""集成测试：跨仓库完整管线（ParallelOrchestrator.run + MergeOrchestrator.merge）。

不调真 Kiro——把 Implementor 那步（_run_one_task）stub 成"写 files_owned 文件 +
返回 PASS"，但 worktree 创建、波次调度、按 repo 路由、run-state、per-repo merge
全部走真实代码路径。

场景（对应 ROADMAP M2 "主仓库 + 二级仓库同时改"）：
  主仓库 main_repo: core（repo 缺省）
  二级仓库 api:     api-handler（依赖 core）
  二级仓库 web:     web-client（依赖 core）
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.dag import load_workspace
from kiro_conduit.merge import MergeOrchestrator
from kiro_conduit.orchestrator import ParallelOrchestrator
from kiro_conduit.roles.coordinator import CoordinatorOutcome
from kiro_conduit.types import LayerResult, TaskResult, VerifyLayer, VerifyResult


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, capture_output=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _passing_outcome(task_id: str) -> CoordinatorOutcome:
    tr = TaskResult(task_id=task_id, success=True, diff="", files_changed=[])
    vr = VerifyResult(
        task_id=task_id,
        passed=True,
        layers=[LayerResult(layer=VerifyLayer.STATIC, passed=True, output="ok")],
        feedback="ok",
    )
    return CoordinatorOutcome(
        task_id=task_id,
        passed=True,
        attempts=1,
        last_task_result=tr,
        last_verify_result=vr,
        history=[(tr, vr)],
    )


@pytest.mark.asyncio
async def test_cross_repo_full_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_repo = tmp_path / "main_repo"
    _init_repo(main_repo)
    api = tmp_path / "api"
    _init_repo(api)
    web = tmp_path / "web"
    _init_repo(web)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    body = dedent(
        f"""
        repos:
          api: {api}
          web: {web}
        phases:
          - name: contract
            type: serial
            tasks: [core]
          - name: build
            type: parallel
            tasks: [api-handler, web-client]
        tasks:
          core: {{spec: specs/core.md, files_owned: ["core.py"]}}
          api-handler:
            spec: specs/h.md
            repo: api
            depends_on: [core]
            files_owned: ["handler.py"]
          web-client:
            spec: specs/c.md
            repo: web
            depends_on: [core]
            files_owned: ["client.ts"]
        shared_files: []
        """
    ).lstrip()
    (ws_dir / "dag.yaml").write_text(body, encoding="utf-8")
    specs = ws_dir / "specs"
    specs.mkdir()
    for n in ("core", "h", "c"):
        (specs / f"{n}.md").write_text("spec\n")
    ws = load_workspace(ws_dir / "dag.yaml")

    owned = {"core": "core.py", "api-handler": "handler.py", "web-client": "client.ts"}

    async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
        async with sem:
            wt = await wm.create(task_def.id, base_branch=base_branch)
            (wt.path / owned[task_def.id]).write_text(f"// {task_def.id}\n")
            return _passing_outcome(task_def.id)

    monkeypatch.setattr(ParallelOrchestrator, "_run_one_task", fake)

    # base_repo = 主仓库（core 落这里；api/web task 路由到各自仓库）
    report = await ParallelOrchestrator(ws, base_repo=main_repo).run()
    assert report.all_passed
    assert set(report.outcomes) == {"core", "api-handler", "web-client"}

    successful = {tid for tid, o in report.outcomes.items() if o.passed}
    merge_report = await MergeOrchestrator(ws, base_repo=main_repo).merge(
        handles=report.handles, successful_task_ids=successful
    )
    assert merge_report.all_merged

    # 各仓库 main 拿到自己 task 的文件，互不串
    assert (main_repo / "core.py").is_file()
    assert (api / "handler.py").is_file()
    assert (web / "client.ts").is_file()
    assert not (api / "core.py").exists()
    assert not (web / "core.py").exists()
    assert not (main_repo / "handler.py").exists()
