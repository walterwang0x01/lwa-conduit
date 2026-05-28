# kiro-conduit

> 把一份大 spec 拆成 DAG，让多个 [Kiro CLI](https://github.com/kirodotdev/Kiro) 实例在 git worktree 里并行干活，最后串行 merge 回主分支。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-red)

## 这是什么

**kiro-conduit** 是一个面向 Kiro CLI 的并行 spec 执行编排器。

它解决一个具体痛点：

> 你写了一份很大的 spec（几十个 PR、跨多个仓库），Kiro 一个一个串行做，跑了几天还没收敛。你手动开 5 个分支并行做，结果 merge 时全是冲突。

kiro-conduit 把这件事变成：

```
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
│   Verifier   │ ◄── 静态/动态/语义 │   diff +    │
└──────┬───────┘                     │   tests     │
       │                             └─────────────┘
       ▼
   按 DAG 顺序串行 merge 回 main
```

## 为什么不直接用 Cursor / Claude Code / Conductor / Intent

简单：**它们都不支持 Kiro CLI**。市面上 9+ 个开源/商业并行编排器全部绑定 Cursor、Claude Code 或 Augment 自家 agent，没有一个用 Kiro 的 [ACP 协议](https://agentclientprotocol.com/)。

如果你已经在用 Kiro，又想要并行编排，这个项目是目前唯一选项。

## 设计原则

kiro-conduit 不发明新模式，它把 2026 年行业共识的**6 大并行编排模式**落到 Kiro 生态：

1. **Spec-Driven Decomposition** —— spec 是唯一真相，agent 不自由发挥
2. **Git Worktree Isolation** —— 每个 agent 自己的工作目录，物理隔离
3. **Coordinator / Implementor / Verifier (CIV)** —— 三角色分工，单 agent 不可能写好规划+执行+审查
4. **BYOA Model Routing** —— Coordinator 用强模型，Implementor 用便宜模型
5. **Multi-Layer Verification** —— lint → test → AI review，便宜的检查在前
6. **Sequential Merge** —— 串行 merge + rebase，不试图自动解语义冲突

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 状态

🚧 **Pre-alpha**：当前只有顶层文档，代码 0 行。

正在按 [docs/ROADMAP.md](docs/ROADMAP.md) 推进：

- [ ] M0：PoC，1 个 Coordinator + 1 个 Implementor + 1 个 Verifier 跑通最小链路
- [ ] M1：MVP，DAG 调度 + worktree 池 + 串行 merge
- [ ] M2：实战检验，跑通真实大 spec（跨多模块、跨两仓库的 11 PR 项目）
- [ ] M3：开源发布

## 你可能在找的文档

| 想知道 | 看哪份 |
|--------|--------|
| 这个项目要解决什么问题、不解决什么问题 | [docs/PRD.md](docs/PRD.md) |
| 系统架构、CIV 三角色、6 大模式怎么落地 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| MVP 范围、什么时候做完、为什么这么排 | [docs/ROADMAP.md](docs/ROADMAP.md) |

## 运行环境

- macOS / Linux
- Kiro CLI（已登录，能跑 `kiro-cli acp`）
- Git 2.38+（需要 `git worktree`）
- Python 3.11+ 或 Node 20+（实现语言尚未锁定，见 ROADMAP）

## License

MIT，见 [LICENSE](LICENSE)。

## 相关项目

- [kirodotdev/Kiro](https://github.com/kirodotdev/Kiro) —— 本项目编排的对象
- [Agent Client Protocol](https://agentclientprotocol.com/) —— Kiro CLI 暴露的程序化接口
- [walterwang0x01/lark-kiro-bridge](https://github.com/walterwang0x01/lark-kiro-bridge) —— 同作者的 Kiro ACP 客户端实现，本项目复用其 ACP 通信代码
