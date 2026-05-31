# 使用指南：`kiro-conduit run`

把一份 workspace（含 `dag.yaml`）跑成完整流程：按 DAG 波次并行派给 Kiro 写代码 →
分层验证 → 默认产出可 review 的分支（或 `--merge` 合并）。

## 安装

```bash
pip install -e '.[dev]'        # 开发安装（仓库内）
# 或 pipx install kiro-conduit  （PyPI 发布后）
```

前提：已装并登录 [Kiro CLI](https://github.com/kirodotdev/Kiro)（`kiro-cli` 在 PATH 上）。

## workspace 结构

一个目录，含 `dag.yaml` + 各 task 的 spec markdown：

```text
my-workspace/
  dag.yaml
  specs/
    task-a.md
    task-b.md
```

`dag.yaml` 最小示例：

```yaml
phases:
  - name: build
    type: parallel
    tasks: [task-a, task-b]
tasks:
  task-a:
    spec: specs/task-a.md
    files_owned: ["src/a.py"]
    acceptance:
      - "python3 -m py_compile src/a.py"
      - "pytest -q tests/test_a.py"
  task-b:
    spec: specs/task-b.md
    files_owned: ["src/b.py"]
    acceptance: ["pytest -q tests/test_b.py"]
shared_files: []
```

跨仓库见 [`examples/dags/cross-repo.yaml`](../examples/dags/cross-repo.yaml)。

## 基本用法

```bash
kiro-conduit run --workspace my-workspace/
```

默认是 **review 模式**：跑完只把每个通过的 task 留在 `kiro-conduit/<task>` 分支上，
打印分支清单 + 如何 diff，**不自动合并**。你 review 后再决定合不合。

要合并回 base 分支：

```bash
kiro-conduit run --workspace my-workspace/ --merge
```

## 生产安全行为（重要）

- **base 分支默认 = 仓库当前分支**，不写死 `main`。用 `--base-branch` 覆盖。
- **绝不动你的主工作区 / 当前分支**：合并在一个独立的 integration worktree 里做。
  - base 分支没被检出 → 在其上推进。
  - base 分支正是你当前所在分支 → 结果合到 `kiro-conduit/integration` 分支，
    你的分支和工作区完全不动，事后自行 review 再合（review-and-accept）。
- **脏工作区是安全的**：worktree 从已提交的 HEAD 起，你未提交的改动不受影响。
  启动预检会打印当前分支 / 脏区状态 / base / 去向。
- **非 git 仓库会在预检阶段直接报错**，不会做半截。

## 常用 flags

| flag | 作用 |
|------|------|
| `--workspace <dir>` | 含 `dag.yaml` 的目录（或直接传 `dag.yaml` 路径） |
| `--base-repo <dir>` | 目标 git 仓库（默认 = workspace 目录） |
| `--base-branch <name>` | base 分支（默认 = 仓库当前分支） |
| `--merge` | 合并通过的 task 分支回 base（默认不合，只产出分支供 review） |
| `--resume` | 从上次 run-state 续跑，已通过的 task 不重跑 |
| `--dashboard` | rich 实时 TUI（wave / worker / 锁 / merge 状态） |
| `--diagnose` | merge 冲突时产出结构化诊断（冲突文件 + 内容） |
| `--max-concurrency N` | 同波次最大并发（默认 4） |
| `--max-attempts N` | 单 task 失败重试上限（默认 3） |
| `--kiro-cli <path>` | kiro-cli 可执行文件路径（默认 `kiro-cli`） |

## 运行时隔离（并行跑测试不撞）

并行 task 各自跑 acceptance 命令时共享端口/DB/状态会静默冲突。每个 task 的验证命令
会拿到一组确定性、不冲突的环境变量，**在你的测试/应用配置里读它们**即可隔离：

| 变量 | 含义 |
|------|------|
| `KIRO_CONDUIT_TASK_ID` | task 标识（可作 DB 名 / 资源前缀后缀） |
| `KIRO_CONDUIT_PORT_BASE` | 不重叠的端口区间起点（base + 稳定索引×100） |
| `KIRO_CONDUIT_SCRATCH` | 每个 task 独立的 scratch 目录（已创建，放临时 DB/文件） |

示例（pytest 里按 task 选端口/库名）：

```python
import os
PORT = int(os.environ.get("KIRO_CONDUIT_PORT_BASE", "8000"))
DB_NAME = f"test_{os.environ.get('KIRO_CONDUIT_TASK_ID', 'local')}"
```

base 端口可在编排器里用 `ParallelOrchestrator(isolation_base_port=...)` 调整。

## 断点续跑

跑到一半崩了，加 `--resume` 重跑：已通过的 task 从其分支重建、不重跑，从未完成处继续。
进度记录在 `<base_repo>/.kiro-conduit/run-state.json`。
