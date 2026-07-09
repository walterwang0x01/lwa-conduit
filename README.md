<!-- markdownlint-disable MD033 MD041 -->

<h1 align="center">Conduit（kiro-conduit）</h1>

<p align="center">
  <strong>Lark Local Agent Workbench（LWA）</strong> 的 DAG 编排与角色执行层
</p>

<p align="center">
  <em>把一份大 spec 拆成 DAG，让多个本地 Agent CLI（<a href="https://github.com/kirodotdev/Kiro">Kiro</a> / Cursor 等）在 git worktree 里按角色并行干活，最后串行 merge 回主分支。</em>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
  <a href="https://github.com/walterwang0x01/lwa-conduit/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/walterwang0x01/lwa-conduit/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="status: M2 in progress" src="https://img.shields.io/badge/status-M2%20in%20progress-green">
  <img alt="tests" src="https://img.shields.io/badge/tests-304%20passed-brightgreen">
  <img alt="ruff" src="https://img.shields.io/badge/ruff-clean-brightgreen">
  <img alt="mypy" src="https://img.shields.io/badge/mypy--strict-clean-brightgreen">
  <img alt="python" src="https://img.shields.io/badge/python-3.11%2B-blue">
</p>

---

## 目录

- [它解决什么](#它解决什么)
- [Quick Start：5 分钟跑通](#quick-start5-分钟跑通)
- [Demo 输出](#demo-输出)
- [Why kiro-conduit：为什么不用 X](#why-kiro-conduit为什么不用-x)
- [设计原则：6 大模式](#设计原则6-大模式)
- [实测数据](#实测数据)
- [项目状态](#项目状态)
- [文档](#文档)
- [License](#license)

---

## 它解决什么

> 你写了一份很大的 spec（几十个 PR、跨多个仓库），Kiro 一个一个串行做，跑了几天还没收敛。你手动开 5 个分支并行做，结果 merge 时全是冲突。

Conduit（kiro-conduit）把这件事变成：

```text
spec.md
   │
   ▼
┌──────────────┐
│ Coordinator  │  读 spec → 生成 DAG → 派发任务
└──────┬───────┘
       │
       ├──── worktree-A ──── kiro-cli acp ──┐
       ├──── worktree-B ──── kiro-cli acp ──┤   ← 并行执行
       ├──── worktree-C ──── kiro-cli acp ──┤
       │                                    │
       ▼                                    ▼
┌──────────────┐                     ┌─────────────┐
│   Verifier   │ ◄── 静态 / 动态 / 语义 │   diff +    │
└──────┬───────┘                     │   tests     │
       │                             └─────────────┘
       ▼
   按 DAG 顺序串行 merge 回 main
```

---

## Quick Start：5 分钟跑通

> 前提：已装 [Kiro CLI](https://github.com/kirodotdev/Kiro) 并 `kiro-cli login` 完成。

```bash
# 1. 克隆
git clone https://github.com/walterwang0x01/lwa-conduit.git
cd kiro-conduit

# 2. 装依赖（venv）
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 3. 跑测试套件（不调真 Kiro，2 秒跑完）
pytest

# 4. 跑 M0 端到端 demo（真调 1 次 Kiro，约 25 秒，单任务）
python examples/02_civ_hello.py

# 5. 跑 M1.0 端到端 demo（真调 3 次 Kiro，约 2-4 分钟）
#    DAG: pkg-base 串行 → pkg-mul + pkg-sub 并行（共享 src/calc/__init__.py）
python examples/03_m1_demo.py

# 6. 跑 M1.1 stub-first demo（真调 4 次 Kiro，约 3-5 分钟）
#    比 M1.0 多一个 pkg-stub task 锁定 __init__.py 接口，consumer 不能改
#    M1.0 那个文本冲突在这个版本里**完全消失**
python examples/04_m1_stub_first_demo.py

# 7. 想验证稳定性？连跑 5 次 M0 demo（约 2 分钟）
python tools/stability_run.py 5
```

第 5/6 步是**真正的并行编排**：会建多个 git worktree，并行跑 Kiro CLI（带共享
文件锁 + 接口契约校验），最后串行 merge 回 main。M1.1 step 6 加上 stub-first 后，
多个 task 改同共享文件的失败模式从设计上消除了。

**想看 TUI dashboard？** 加环境变量启用（M1.1 step 4）：

```bash
KIRO_CONDUIT_DASHBOARD=1 python examples/04_m1_stub_first_demo.py
```

会用 [rich.live](https://rich.readthedocs.io/en/stable/live.html) 实时显示 wave 进度、
每个 worker 状态、共享文件锁 holder、merge 状态。订阅自 EventBus，跟 orchestrator
解耦——不开启时行为完全不变。

---

## Demo 输出

`python examples/02_civ_hello.py` 的真实输出（节选）：

```text
✓ Created test repo: /var/folders/.../kiro-conduit-demo-abc123

[coordinator] task=add-function attempt=1/2
[implementor] start task=add-function cwd=/.../kiro-conduit-demo-abc123
[implementor] task=add-function turn ended (stop=end_turn)
[verifier static] $ python3 -m py_compile calc.py test_calc.py
[verifier dynamic] $ pytest -q test_calc.py
[coordinator] task=add-function PASSED on attempt 1

============================================================
Task: add-function
Passed: True
Attempts: 1
Files changed: ['calc.py', 'test_calc.py']

Verifier layers:
  ✓ static
  ✓ dynamic
  ✓ semantic (skipped)
  ✓ contract (skipped)
```

Implementor 写出的代码（一次过，没改一行）：

```python
# calc.py
"""简易计算工具模块。"""


def add(a: int, b: int) -> int:
    """返回两个整数的和。"""
    return a + b
```

```python
# test_calc.py
from calc import add


def test_add_positive_numbers():
    assert add(2, 3) == 5


def test_add_with_zero():
    assert add(0, 0) == 0
    assert add(0, 7) == 7


def test_add_negative_numbers():
    assert add(-1, -1) == -2
    assert add(-5, 3) == -2
```

### M1.0 多任务并行 demo

`python examples/03_m1_demo.py` 的真实输出（节选）：

```text
✓ Demo workspace: /var/folders/.../kiro-conduit-m1-demo-...
✓ Loaded DAG: 3 tasks, 2 phases, 1 shared file(s)
  Tasks: ['pkg-base', 'pkg-mul', 'pkg-sub']

==================================================================
Phase 1: ParallelOrchestrator running (Implementor + Verifier per task)
==================================================================
[orchestrator] 2 waves total: [1, 2]
[orchestrator] wave 1/2: running ['pkg-base'], skipping []
[worktree] created task=pkg-base path=.../worktrees/pkg-base branch=kiro-conduit/pkg-base
[coordinator] task=pkg-base PASSED on attempt 1
[orchestrator] wave 2/2: running ['pkg-mul', 'pkg-sub'], skipping []
[lock] task=pkg-mul acquired src/calc/__init__.py     ← 共享文件锁 acquired
[lock] task=pkg-mul released src/calc/__init__.py     ← release 后 pkg-sub 立即 acquire
[lock] task=pkg-sub acquired src/calc/__init__.py
[coordinator] task=pkg-mul PASSED on attempt 1
[coordinator] task=pkg-sub PASSED on attempt 1

✓ Parallel phase done in 125.5s
  ✓ pkg-base: passed=True, attempts=1, files_changed=2
  ✓ pkg-mul:  passed=True, attempts=1, files_changed=2
  ✓ pkg-sub:  passed=True, attempts=1, files_changed=2

==================================================================
Phase 2: MergeOrchestrator running (serial merge in topological order)
==================================================================
[merge] order: ['pkg-base', 'pkg-mul', 'pkg-sub']

✓ Merge phase done in 0.6s
  ✓ pkg-base
  ✓ pkg-mul
  ✗ pkg-sub — merge kiro-conduit/pkg-sub conflicted in src/calc/__init__.py
              (M1.0 design: text conflicts stop and require human review)

  Running pytest -q on main...
  6 passed in 0.02s     ← main 上 add + mul 测试全过
```

注意 `pkg-sub` 那次冲突——这是 M1.0 的**预期行为**：`pkg-mul` 和 `pkg-sub` 都
在 `__init__.py` 末尾追加一行（不同的 import），git 无法判断这两改动可以共存。
M1.0 的设计契约就是**遇到文本冲突停下，交人工解决**（行业共识：自动语义合并不可
靠）。M1.1 会用 stub-first 接口锁定从根本上避免这种共享文件冲突。

### M1.1 stub-first demo（M1.0 那个 conflict 不再发生）

`python examples/04_m1_stub_first_demo.py` 的真实输出（节选）：

```text
✓ Loaded DAG: 4 tasks, 2 phases
  Interface locks: ['pkg-stub owns src/calc/__init__.py for [pkg-mul, pkg-sub]']
  Tasks: ['pkg-base', 'pkg-mul', 'pkg-stub', 'pkg-sub']

==================================================================
Phase 1: ParallelOrchestrator (with stub-first interface lock)
==================================================================
[orchestrator] 3 waves total: [1, 1, 2]
  wave 1: pkg-base                       ← phase A
  wave 2: pkg-stub                       ← interface_lock 衍生的 owner 子波次
  wave 3: pkg-mul + pkg-sub (parallel)   ← consumers，从 pkg-stub 分支起 worktree

[coordinator] task=pkg-mul attempt=1/2  ← 试图改 __init__.py
[coordinator] task=pkg-mul attempt 1 failed: [contract failed]
[coordinator] task=pkg-mul attempt=2/2  ← 加上反馈重试，这次只改 mul.py
[coordinator] task=pkg-mul PASSED on attempt 2

✓ Parallel phase done in 167.4s
  ✓ pkg-base / pkg-stub / pkg-mul / pkg-sub 全部 PASS

==================================================================
Phase 2: MergeOrchestrator
==================================================================
✓ Merge phase done in 0.4s
  ✓ pkg-base
  ✓ pkg-stub
  ✓ pkg-mul       ← M1.0 在这里冲突
  ✓ pkg-sub       ← M1.0 在这里也冲突

main 上 src/calc/__init__.py:
  """calc package: re-exports add / mul / sub."""
  from src.calc.add import add  # noqa: F401
  from src.calc.mul import mul  # noqa: F401
  from src.calc.sub import sub  # noqa: F401

10 passed in 0.02s

✓ M1.1 stub-first demo SUCCESS — interface lock prevented
  the conflict that broke the M1.0 demo.
```

**关键观察**：

- pkg-mul / pkg-sub 第一次 attempt 都被 Verifier Layer 4 拒了——它们试图改
  `__init__.py`（即使 spec 写了"不要改"），契约校验抓住了越界行为
- 第二次 attempt 带着反馈重做，只改各自的 .py 文件，4/4 task 全过
- **4/4 merge 全部成功**，没有任何 git 冲突——stub-first 从设计上消除了那个失败模式

---

## Why kiro-conduit：为什么不用 X

并行 AI coding 这块 2026 年才成形、周更级 churn，"主流产品"其实分 **5 层**，先把位置摆清：

| 层 | 是什么 | 代表 | 与本项目 |
|----|--------|------|----------|
| 1 IDE/内联助手 | 编辑器里交互式（=vibe coding） | Copilot、Cursor、Windsurf | 不同类 |
| 2 终端 CLI agent | 单个自主 agent 跑命令 | Claude Code、Codex CLI、Gemini CLI、Aider、Amp、**Kiro CLI** | **被驱动的对象** |
| **3 worktree 并行编排器** | 多个第 2 层 agent 并行在 git worktree | Conductor、Vibe Kanban、Claude Squad、Crystal/Nimbalyst、Superconductor、code-conductor、MS Conductor | **kiro-conduit 在这层** |
| 4 云端/异步 agent | 云 VM 异步跑、开 PR | Devin、Cursor Cloud Agents、Codex cloud、Copilot coding agent、Augment Cosmos | 同思路、云托管 |
| 5 沙箱基础设施 | agent 运行的隔离层 | E2B、Daytona、Modal、Firecracker、Vercel Sandbox | `--sandbox` 是其轻量本地版 |

**第 3 层（同类）横评——它们都不原生支持 Kiro CLI**：

| 工具 | 支持的 agent | worktree 隔离 | 自动验证/集成审查 | 跨仓库 |
|------|--------------|--------------|------------------|--------|
| **kiro-conduit** | **Kiro CLI** (ACP) | ✅ | ✅ 4 层验证 + 集成级 AI 初审 | ✅ |
| Conductor | Claude Code / Cursor | ✅ | ✗（留给人 review/merge） | ✗ |
| Vibe Kanban | Claude / Codex / 通用 | ✅ | ✗ | ✗ |
| Claude Squad | Claude Code | ✅ | ✗ | ✗ |
| Crystal / Nimbalyst | Claude Code | ✅ | ✗ | 部分 |
| Superconductor | Claude/Codex/Gemini/通用 | ✅ | ✗ | 部分 |
| code-conductor | Claude Code subagents | ✅ | ✗ | ✗ |

> 行业把第 3 层的玩法叫 **"agentmaxxing"**：尽量多开 agent 并行，人从"写代码"变成"审代码"。多数同类把 **merge/review 全丢给你**——kiro-conduit 多做了一步**自动集成 + AI 初审**，把人收敛到只审一份报告。

**kiro-conduit 的差异化（截至 M2）**：

- ✅ **唯一原生驱动 Kiro CLI（ACP）** —— 第 3 层里独一份
- ✅ **依赖累积**：任务基于其依赖的真实产出工作（不是各自从 base 起重造）
- ✅ **Verifier 4 层**：static(lint) → dynamic(test) → semantic(对照 spec 的 AI review) → contract
- ✅ **集成级 AI 初审**（`--review`）+ **集成全量验证**（`integration_check`）：拼好后自动审 + 构建，人只看一份报告
- ✅ **失败也合并已通过的**到 `kiro-conduit/integration`，绝不碰你工作区/当前分支
- ✅ **按任务选模型**（dag `model:`）、**接口锁定 stub-first**、**3 种共享文件锁**
- ✅ **worktree 环境准备**（`setup` / `copy_files` / `--venv`）、**OS 级写入沙箱**（`--sandbox`，Seatbelt/bwrap）
- ✅ **跨仓库**、**断点续跑**（run-state）、**瞬时错误退避**、**TUI dashboard**

> **趋势提醒（诚实）**：前沿正从本地第 3 层往**云托管第 4 层**（Cursor Cloud Agents、Cosmos "agentic OS"）走，那是为"舰队规模"设计的。kiro-conduit 是**本地、轻、零基础设施**——简单、私有代码不出本机，但不冲规模。
>
> 想看第 3 层开源工具横评？参考 [Augment Code 的 2026 综述](https://www.augmentcode.com/tools/open-source-agent-orchestrators)。

---

## 设计原则：6 大模式

kiro-conduit 不发明新模式，它把 2026 年行业共识的 **6 大并行编排模式**落到 Kiro 生态。这 6 大模式的源头是 Augment Cosmos / VeriMAP (EACL 2026) / Anthropic Multi-Agent Research / Microsoft 等的共识。

1. **Spec-Driven Decomposition** —— spec 是唯一真相，agent 不自由发挥
2. **Git Worktree Isolation** —— 每个 agent 自己的工作目录，物理隔离
3. **Coordinator / Implementor / Verifier (CIV)** —— 三角色分工，单 agent 不可能同时写好规划 + 执行 + 审查
4. **BYOA Model Routing** —— Coordinator 用强模型，Implementor 用便宜模型
5. **Multi-Layer Verification** —— lint → test → AI review，便宜的检查在前
6. **Sequential Merge** —— 串行 merge + rebase，不试图自动解语义冲突

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 实测数据

> 截至 2026-05-29 的 M1.1 done 实测（Kiro CLI Agent v2.4.2，本地 macOS）。
>
> **M2 当前**：测试 **304 passed**（ruff / mypy-strict clean），并在一个真实的多任务
> 后端特性（约 17 任务 / 8 波次）上端到端跑通；下表为各里程碑的历史快照。

| 指标 | M0 | M1.0 | M1.1 step 1 | **M1.1 done** |
|------|----|------|-------------|---------------|
| 单元测试 | 49 | 102 | 139 | **193** |
| 集成测试 | 6 | 6 | 6 | 6 |
| 测试套件总耗时 | 1.93 s | ~7 s | ~7 s | **~6 s** |
| 源代码行数 | ~1100 | ~3000 | ~3300 | **~4500** |
| 源文件 | 10 | 15 | 16 | **19** |
| 单任务 demo 成功率 | 5/5 | 5/5 | — | — |
| 多任务 DAG demo PASS | — | 3/3 task | 4/4 task | 4/4 task |
| 多任务 DAG demo 耗时 | — | ~125 s | ~167 s | ~167 s |
| 共享文件锁 policy | — | single-writer | single-writer | **3 种全实现** |
| stub-first 接口锁定 | — | — | ✅ | ✅ |
| Verifier Layer 数 | 2 | 2 | 3 (含 contract) | **4** (加 semantic) |
| TUI dashboard | — | — | — | **✅ rich.live** |
| BYOA 模型路由 | — | — | — | **✅** |
| 串行 merge 成功率 | — | 2/3 | 4/4 | 4/4 |
| ruff 错误 | 0 | 0 | 0 | 0 |
| mypy strict 错误 | 0 | 0 | 0 | 0 |

实测得到的几个关键工程发现：

- **ACP `protocolVersion` 是整数 `1`**，不是文档可能暗示的日期串 `"2025-01-01"`
- **Kiro 通过 `session/request_permission` 反向请求权限**：写文件 / 跑命令前都会问。客户端不响应就永远阻塞——任何想做 Kiro 编排器的人都会撞上这块"暗礁"
- **`asyncio.create_task` 不存引用会被 GC 回收**（Python 文档明文警告）。修法：放进 `set` + `add_done_callback(set.discard)`
- **Git worktree 的 `.git/info/exclude` 不是 worktree-local 的**，它共享 base repo 的——想 worktree 级别过滤构建产物（`__pycache__/`、`*.pyc`），得在 `git add` 时用 pathspec `:(exclude)`，而不是写 info/exclude
- **共享文件单一写者锁工作正确**：M1.0 demo 日志显示 `pkg-mul acquired → released → pkg-sub acquired` 严格串行
- **真实的语义冲突要停下交人工**：pkg-mul/pkg-sub 都追加 `__init__.py` 末尾不同 import 时 git 自动 merge 失败，M1.0 按设计停下报告而不是猜测——M1.1 会用 stub-first 接口锁定从根上避开

---

## 项目状态

🚧 当前 **M1.1 done**（M1 MVP 第二阶段全 4 step 完成），下一步 M2 实战。

按 [docs/ROADMAP.md](docs/ROADMAP.md) 推进：

- [x] **M0：PoC** — 1 个 Coordinator + 1 个 Implementor + 1 个 Verifier 跑通最小链路（5/5 稳定）
- [x] **M1.0：核心骨架** — DAG 调度 + Git worktree 隔离 + 多 worker 并行 + 共享文件锁（single-writer）+ 串行 merge
- [x] **M1.1：增强能力** — 4 step 全完成：
  - **step 1** Stub-first 接口锁定（解决 M1.0 那个文本冲突）
  - **step 2** Verifier Layer 3（可插拔 AI 语义 review）
  - **step 3** BYOA 模型路由 + 完整锁 policy（append-only / coordinator-only）
  - **step 4** TUI dashboard（rich live + EventBus）
- [ ] **M2：实战** — 真实大 spec 端到端**已跑通**（一个真实的多模块后端特性，约 17 任务 / 8 波次，
  生成 + 集成 + 验证 + 合并），并据此把工具硬化到生产可用：
  - 已落地：跨仓库（repos + per-repo worktree/merge）、断点续跑（run-state）、瞬时错误退避、
    GitHub Actions CI；**依赖累积**（任务基于依赖的真实产出工作）；**按任务选模型**（dag `model:`）；
    **失败也合并已通过的**到 integration；**集成级 AI 初审**（`--review`，对照 spec 审整体 diff）；
    **集成全量验证**（`integration_check`）；**worktree 准备**（`setup` / `copy_files` / `--venv`）；
    **OS 级写入沙箱**（`--sandbox`，Seatbelt/bwrap）
  - **待做**：沙箱实机隔离验证、planner 粒度的真实迭代
- [ ] **M3：开源** — 完整 user guide / CI / PyPI / 公开博客系列

---

## 文档

| 想知道 | 看哪份 |
|--------|--------|
| LWA 跨项目季度路线图 | [docs/ROADMAP-LWA.md](docs/ROADMAP-LWA.md) |
| LWA 对外介绍（30 秒 pitch） | [docs/PITCH.md](docs/PITCH.md) |
| 阶段 B 仓库/包名重命名规划（B3 已完成，包名未改） | [docs/REPO_RENAME_PLAN.md](docs/REPO_RENAME_PLAN.md) |
| LWA 体系总览、Bridge 与 Conduit 分工 | [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) |
| 生产级多 CLI / 角色路由与 adaptive | [docs/runtime-routing.md](docs/runtime-routing.md) |
| 这个项目要解决什么问题、不解决什么问题 | [docs/PRD.md](docs/PRD.md) |
| 系统架构、CIV 三角色、6 大模式怎么落地 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| MVP 范围、什么时候做完、为什么这么排 | [docs/ROADMAP.md](docs/ROADMAP.md) |
| 怎么用 `kiro-conduit run` 跑自己的 spec（CLI / flags / 安全行为 / 运行时隔离） | [docs/USAGE.md](docs/USAGE.md) |

## 运行环境

- macOS / Linux
- Kiro CLI（已登录，能跑 `kiro-cli acp`）
- Git 2.38+（需要 `git worktree`）
- Python 3.11+

## License

MIT，见 [LICENSE](LICENSE)。

---

## 相关项目

- [Bridge（lark-kiro-bridge）](https://github.com/walterwang0x01/lwa-bridge) —— LWA 飞书入口；飞书里可用 `/conduit` 触发本编排器
- [kirodotdev/Kiro](https://github.com/kirodotdev/Kiro) —— 支持的 Agent CLI 之一
- [Agent Client Protocol](https://agentclientprotocol.com/) —— Kiro CLI 暴露的程序化接口
