# PRD：kiro-conduit

> 受众：项目维护者 / 早期使用者 / 想了解项目定位的人
>
> 作用：回答"这个项目到底要解决什么、不解决什么、做到什么样子算成功"

---

## 1. 起源：一个真实的痛点

2026 年 5 月，我（Walter）在做一个跨多模块、跨两个仓库的大型后端系统改造，spec 拆成了 9 个阶段、约 18 个 PR，总工时预估 9-13 天。

**问题是：Kiro 一个一个串行做，跑了两天还在中段，剩余 11 个 PR 看着像跑不完。**

我尝试手动并行：开 5 个 git worktree、5 个分支，各自跑 Kiro IDE 实施。问题立刻暴露：

1. **冲突频发**：多个分支改了 `constants.py` / `db_init.py` / `main.py` 这种"hub 文件"
2. **接口飘移**：多个并行 PR 都依赖同一个核心 builder 类，但各自实现的接口签名不一致，merge 后才发现
3. **review 流程靠手**：每个 PR 都要我手动起 Kiro CLI 跑 review，写 round1/round2 prompt
4. **进度没大盘**：5 个窗口在跑，我得来回切 IDE 看哪个卡住了
5. **跨仓库尴尬**：Kiro 一个 session 一个 cwd，两个仓库不能同时改

我当时手写了一份"多窗口并行 spec"做调度指引，但这本质是**手动模拟一个并行编排器**——用人脑做 DAG 调度、用约定做共享文件锁、用 stash 做隔离。

**kiro-conduit 就是把这个手动流程自动化。**

## 2. 解决什么

### 2.1 核心目标

**给定一份大 spec，自动并行执行，最终产出一组可串行 merge 的 PR。**

具体落到 6 件事：

| # | 用户痛点 | kiro-conduit 提供 |
|---|---------|-------------------|
| 1 | spec 太大，Kiro 一个 session 串行跑跑不完 | spec → DAG 拆解 → 多 worker 并行 |
| 2 | 多 worktree 改 hub 文件冲突 | 共享文件单一写者机制 + 提前预警 |
| 3 | 并行任务之间接口不一致 | stub-first：先冻结接口再并行实现 |
| 4 | review 靠手动起 reviewer + 写 prompt | 内置 Verifier 流水线（lint/test/AI review） |
| 5 | 多 worker 进度不可见 | 终端 dashboard 聚合状态 |
| 6 | 跨仓库不能并行 | 一个 workspace 抽象，多 repo 各自 worktree |

### 2.2 第一性原则

kiro-conduit 不发明新模式，**严格遵循 2026 年行业共识的 6 大模式**（详见 ARCHITECTURE.md）：

1. Spec-Driven Decomposition
2. Git Worktree Isolation
3. Coordinator / Implementor / Verifier
4. BYOA Model Routing
5. Multi-Layer Verification
6. Sequential Merge

任何不符合这 6 个模式的"创新"都要重新审视。

## 3. 不解决什么（明确划线）

下面这些**故意不做**，每条都标了原因：

| 不做的事 | 原因 |
|---------|------|
| **不做云端 SaaS 托管编排** | 项目定位仍然是本机 / 自管运行时编排，不做托管执行平台 |
| **不做 Web UI** | 终端 TUI 够用，参考 Kiro 的 Ctrl+G。做 Web 是过早的产品化 |
| **不做自动语义冲突解决** | 行业共识：git 检测文本冲突，语义冲突必须人来。试图自动解 = 引入更多 bug |
| **不做 LLM 自动拆 spec** | MVP 只接受**用户写好的 DAG**（YAML/JSON）。LLM 拆 spec 质量不稳定，先把执行链路跑通 |
| **不做跨机器分布式** | MVP 单机多进程足够。worktree 多了磁盘吃紧再说 |
| **不做共享 build 缓存** | 每个 worktree 独立装依赖，慢但可靠。优化等真痛了再说 |
| **不做 SDK / 多语言绑定** | 一个 CLI 工具够了。SDK 抽象需要 ≥ 2 客户验证才靠谱 |
| **不做 spec 可视化编辑器** | Spec 用 Markdown + YAML。可视化编辑是 IDE 的事 |
| **不做 PR 自动创建** | git 命令脚本就行，不要包装 GitHub/GitLab 客户端 |

## 4. 受众

### 4.1 主要用户（必须服务好）

**有大型多模块 spec、想用 Kiro 并行加速的工程师。** 典型画像：

- 后端工程师，项目跨多模块或多仓库
- 已经在用 Kiro CLI / IDE，至少跑过 1 个完整 spec
- 一个人或小团队，没有专职 DevOps 维护编排平台
- 痛过"开多个 worktree 手动并行 + 合并冲突"

### 4.2 次要用户（用得上但不优化）

- **想学 CIV / 多 agent 编排的开发者**：项目代码是教科书
- **要做 AI coding 内容的创作者**：用本项目的真实落地案例做素材
- **Kiro 团队 / 社区**：可作为 ACP 协议消费方的参考实现

### 4.3 不是受众（用不上别勉强）

- 单 agent 跑得动的小项目（小于 5 个 PR / 少于 3 个文件）
- 现有 Cursor / Claude Code 工作流跑得通的人
- 不熟悉 git worktree 的初学者（学习曲线陡）

## 5. 成功标准（怎么算 MVP 跑通）

MVP 必须能完成**一次真实演示**：

> 输入大型项目剩余 11 PR 的 master-plan.md
> ↓
> kiro-conduit 自动起 4 个并行 worker（Phase B）
> ↓
> 4 个 PR 各自完成 + 通过 Verifier
> ↓
> 串行 merge 回 main，无 git 冲突，集成测试通过
> ↓
> 总耗时 ≤ 串行 Kiro 的 50%

**量化指标**：

| 指标 | MVP 目标 | 长期目标 |
|------|---------|---------|
| 端到端跑通真实 spec | ✅ Phase B（4 PR 并行） | 全 spec（11 PR） |
| 节省时间比 | ≥ 50% | ≥ 70% |
| 共享文件冲突自动检测命中率 | ≥ 80% | ≥ 95% |
| Verifier 误报率（false positive） | ≤ 20% | ≤ 5% |
| 每个 worker 单次重试上限 | 3 次（VeriMAP 默认） | 可配置 |
| 内存占用（5 worker 同时跑） | ≤ 8 GB | ≤ 4 GB |

## 6. 反目标（明确不追求的事）

| 反目标 | 为什么 |
|--------|--------|
| 通用编排器（支持任何 agent） | 通用 = 平庸。先在 Kiro 生态做到极致 |
| 商业化 / 收费 | 开源工具，盈利不是目的。如果要赚钱，靠它做的内容和影响力 |
| 跟 Cursor / Claude Code 抢用户 | 它们用户群早稳定，硬抢没胜算 |
| 完美的拆分智能 | 人写 DAG 比 LLM 拆出来的可靠 |
| 解决所有并行编排难题 | 关注 Kiro + 单机多进程这个细分场景 |

## 7. 关键决策记录（ADR）

### ADR-001：实现语言

**决策**：MVP 用 **Python 3.11+**

**原因**：
- 同作者的 [lark-kiro-bridge](https://github.com/walterwang0x01/lark-kiro-bridge) 已实现 ACP 客户端（虽然是 TypeScript），Python 重写成本可控
- ACP 的 JSON-RPC + 子进程管理在 Python 里成熟（asyncio + subprocess）
- 用户群（Python 后端工程师）多
- 备选 Rust 留给后续性能瓶颈出现时

**反对意见回应**：
- "TS 复用 lark-kiro-bridge 不是更快？" → ACP 协议很简单，重写不是瓶颈。Python 在 spec 解析、文件操作、TUI（textual / rich）上更顺手
- "Rust 不是更稳？" → 当前项目核心是子进程编排和 JSON 处理，性能不是瓶颈

### ADR-002：DAG 来源

**决策**：MVP 只接受**用户手写的 DAG（YAML 格式）**，不做 LLM 自动拆分

**原因**：
- 用户痛点的真实部分是"执行调度"，不是"自动拆分"
- LLM 拆出来的 DAG 质量不稳定，会引入新 bug
- 真实项目的 master-plan 实战已证明：人能写出高质量 DAG

**M2 阶段重新评估**：跑通真实 spec 后，看是否需要 LLM 辅助生成 DAG 草稿（人工 review 后采纳）。

### ADR-003：Verifier 实现层

**决策**：Verifier 内部分 **4 层流水线**，便宜的在前贵的在后

```
Layer 1: 静态检查（ruff / mypy / eslint）—— 秒级
Layer 2: 动态测试（pytest / jest）—— 分钟级
Layer 3: AI 语义 review —— LLM 调用，token 成本最高
Layer 4: 接口契约校验 —— 检查 stub-first 阶段定义的接口是否被遵守
```

**原因**：
- 行业共识：deterministic 检查在前，能省 80% LLM 调用
- 失败短路：Layer 1 挂了就不走后面，节省 token

### ADR-004：Merge 策略

**决策**：**串行 merge + 自动 rebase + 冲突时停下让人解**

**原因**：
- Augment / VeriMAP / Anthropic 共识：自动语义冲突解决不可靠
- 人工解冲突的痛苦 < AI 错误合并导致的隐 bug

### ADR-005：与 Kiro CLI 的耦合方式

**决策**：通过 **`kiro-cli acp` 子进程 + ACP JSON-RPC** 通信

**原因**：
- ACP 是 Kiro 官方暴露的程序化接口（[文档](https://agentclientprotocol.com/)）
- 流式状态推送（AgentMessageChunk / ToolCall / TurnEnd）刚好对应 dashboard 需求
- 不依赖 Kiro 内部 API，Kiro 升级不会破坏

**风险**：ACP 协议如果变更，需要适配。缓解：把 ACP 客户端封装成独立模块，未来切其他协议成本可控。

## 8. 开放问题（实施时要回答）

| # | 问题 | 触发时机 |
|---|------|---------|
| Q1 | 共享文件锁用文件系统（`.kiro-conduit/locks/`）还是 SQLite？ | M1 实施 |
| Q2 | Coordinator 和 Implementor 用同一个 Kiro CLI 模型还是分开配置？ | M0 PoC 阶段评估 |
| Q3 | Verifier 的 AI review 用 Kiro CLI 还是直接调 LLM API？ | M1 实施 |
| Q4 | 跨仓库的 workspace 配置格式（YAML schema）？ | M1 设计 |
| Q5 | dashboard 用 textual 还是 rich live？ | M1 选型 |
| Q6 | Kiro CLI session 复用还是每个任务起新 session？ | M0 实测对比 token 消耗 |

## 9. 文档关系

```
README.md           ← 项目门面，5 分钟讲清楚
docs/PRD.md         ← 你正在读的，回答"做什么 / 不做什么"
docs/ARCHITECTURE.md ← 系统架构，CIV 三角色 + 6 大模式落地细节
docs/ROADMAP.md     ← 里程碑 + MVP 范围 + 阶段计划
```
