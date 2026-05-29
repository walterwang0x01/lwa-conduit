<!-- markdownlint-disable MD033 MD041 -->

<h1 align="center">kiro-conduit</h1>

<p align="center">
  <em>把一份大 spec 拆成 DAG，让多个 <a href="https://github.com/kirodotdev/Kiro">Kiro CLI</a> 实例在 git worktree 里并行干活，最后串行 merge 回主分支。</em>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg"></a>
  <img alt="status: M0 PoC" src="https://img.shields.io/badge/status-M0%20PoC%20done-green">
  <img alt="tests" src="https://img.shields.io/badge/tests-58%20passed-brightgreen">
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

kiro-conduit 把这件事变成：

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
git clone https://github.com/walterwang0x01/kiro-conduit.git
cd kiro-conduit

# 2. 装依赖（venv）
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 3. 跑测试套件（不调真 Kiro，2 秒跑完）
pytest

# 4. 跑端到端 demo（真调一次 Kiro，约 25 秒）
python examples/02_civ_hello.py

# 5. 想验证稳定性？连跑 5 次（约 2 分钟）
python tools/stability_run.py 5
```

跑完第 4 步，你会看到 Implementor 在临时 git repo 里写了 `calc.py` + `test_calc.py`，Verifier 跑了 `python3 -m py_compile` + `pytest -q` 都过了。第 5 步会输出一份成功率 + 耗时的报告。

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

---

## Why kiro-conduit：为什么不用 X

市面上至少有 9 个并行 AI coding 编排器。**它们都不支持 Kiro CLI**。

| 工具 | 类型 | 支持的 agent | 共享文件锁 | 接口锁定 (stub-first) | 跨仓库 |
|------|------|--------------|------------|----------------------|--------|
| **kiro-conduit** | OSS, Python | **Kiro CLI** (ACP) | 计划 M1 | 计划 M1 | 计划 M2 |
| Conductor (YC S24) | macOS app | Claude Code, Cursor | ✗ | ✗ | ✗ |
| Intent (Augment) | VS Code 插件 | Augment + BYOA | ✓ | 部分 | 部分 |
| microsoft/conductor | CLI | Copilot SDK, Anthropic | ✗ | ✗ | ✗ |
| ryanmac/code-conductor | CLI | Claude Code subagents | ✗ | ✗ | ✗ |
| Claude Squad | TUI | Claude Code | ✗ | ✗ | ✗ |
| Vibe Kanban | Web | Claude / Codex / 通用 | ✗ | ✗ | ✗ |
| Devin | SaaS | Devin only | ✓ | ✗ | ✗ |
| Cursor Background Agents | IDE | Cursor only | ✗ | ✗ | ✗ |
| GitHub Spec Kit | 模板 | Copilot | — | — | — |

**kiro-conduit 的差异化**：

- ✅ **唯一原生支持 Kiro ACP 协议**，可以直接驱动 `kiro-cli acp` 子进程
- 🟡 **接口锁定 stub-first** 是行业未明确做到的细分能力（Augment Intent 部分实现，多数没做）
- 🟡 **共享文件单一写者锁** + **跨仓库** 在 M1/M2 路线图

> 想看 9 个开源编排器的横评？参考 [Augment Code 那篇 2026 综述](https://www.augmentcode.com/tools/open-source-agent-orchestrators)。

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

> 截至 2026-05-29 的 M0 PoC 实测（Kiro CLI Agent v2.4.2，本地 macOS）。

| 指标 | 实测 | 备注 |
|------|------|------|
| 单元测试数量 | 49 | tests/unit/ |
| 集成测试数量 | 6 | tests/integration/ + mock ACP server |
| 测试套件总耗时 | 1.93 s | pytest 全量，CI 友好 |
| 端到端 demo 成功率 | 5 / 5 (100%) | tools/stability_run.py 5 |
| 端到端 demo 平均耗时 | 25.2 s | min 19.1s / max 28.3s |
| 端到端 demo 重试次数 | 0 | acceptance 写明 `python3` 后无重试 |
| ruff 错误 | 0 | 已忽略中文标点警告（RUF001/002/003） |
| mypy strict 错误 | 0 | 全 10 个源文件 |

实测得到的几个关键工程发现：

- **ACP `protocolVersion` 是整数 `1`**，不是文档可能暗示的日期串 `"2025-01-01"`
- **Kiro 通过 `session/request_permission` 反向请求权限**：写文件 / 跑命令前都会问。客户端不响应就永远阻塞——任何想做 Kiro 编排器的人都会撞上这块"暗礁"
- **`asyncio.create_task` 不存引用会被 GC 回收**（Python 文档明文警告）。修法：放进 `set` + `add_done_callback(set.discard)`，标准做法

---

## 项目状态

🚧 当前 **M0 PoC done**，正在准备 M1。

按 [docs/ROADMAP.md](docs/ROADMAP.md) 推进：

- [x] **M0：PoC** — 1 个 Coordinator + 1 个 Implementor + 1 个 Verifier 跑通最小链路（5/5 稳定）
- [ ] **M1：MVP** — DAG 调度 + worktree 池 + 共享文件锁 + 接口锁定 + TUI dashboard + 串行 merge
- [ ] **M2：实战** — 跑通真实大 spec（跨多模块 + 跨两仓库的 11 PR 项目）
- [ ] **M3：开源** — README / 示例 / CI / PyPI / 公开博客

---

## 文档

| 想知道 | 看哪份 |
|--------|--------|
| 这个项目要解决什么问题、不解决什么问题 | [docs/PRD.md](docs/PRD.md) |
| 系统架构、CIV 三角色、6 大模式怎么落地 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| MVP 范围、什么时候做完、为什么这么排 | [docs/ROADMAP.md](docs/ROADMAP.md) |

## 运行环境

- macOS / Linux
- Kiro CLI（已登录，能跑 `kiro-cli acp`）
- Git 2.38+（需要 `git worktree`）
- Python 3.11+

## License

MIT，见 [LICENSE](LICENSE)。

---

## 相关项目

- [kirodotdev/Kiro](https://github.com/kirodotdev/Kiro) —— 本项目编排的对象
- [Agent Client Protocol](https://agentclientprotocol.com/) —— Kiro CLI 暴露的程序化接口
- [walterwang0x01/lark-kiro-bridge](https://github.com/walterwang0x01/lark-kiro-bridge) —— 同作者的 Kiro ACP 客户端实现
