# Roadmap：kiro-conduit

> 受众：项目维护者 / 早期贡献者
>
> 作用：回答"分几步走、每步做什么、什么时候能演示给人看"

---

## 总览

```
M0 PoC          ────►  M1 MVP         ────►  M2 实战         ────►  M3 开源
最小链路打通          多 worker 并行         真实大 spec 跑通       公开发布

约 1 周               约 2 周                约 1 周                约 0.5 周
```

每个里程碑都有**可演示的产物**和**明确的验收标准**。不达标不进入下一阶段。

---

## M0：PoC（最小链路）

**目标**：证明 Coordinator → Implementor → Verifier 的链路在 Kiro CLI 上能跑通。

**范围**：1 个 Coordinator + 1 个 Implementor + 1 个 Verifier，**串行执行 1 个任务**。

### 必做

- [ ] 项目骨架（pyproject.toml / 目录结构 / 基础 CLI 入口）
- [ ] ACP 客户端模块（`kiro_conduit/acp/client.py`）
  - 起 `kiro-cli acp` 子进程
  - JSON-RPC initialize / session/new / session/prompt
  - 解析 streaming 通知（AgentMessageChunk / ToolCall / TurnEnd）
  - 优雅关闭
- [ ] 极简 spec → task 转换
  - 接受 1 份 markdown spec，固定包成 1 个任务
  - 不做 DAG（M0 暂时只跑串行 1 个任务）
- [ ] Coordinator 角色
  - 读 spec → 派发给 Implementor
  - 等 Implementor 完成 → 派发给 Verifier
  - 收 Verifier 结果 → 决定 PASS / 重试 / 失败退出
- [ ] Implementor 角色
  - 在指定 cwd 跑 `kiro-cli acp`
  - 接收任务上下文 → 写代码 → 输出 git diff
- [ ] Verifier 角色
  - 只做 Layer 1 + Layer 2（静态 + 动态测试）
  - Layer 3/4 暂不做
- [ ] 重试机制（最多 3 次）

### 不做（明确推到后面）

- ❌ 多 Implementor 并行
- ❌ git worktree（M0 直接在主目录跑，能演示就行）
- ❌ 共享文件锁
- ❌ 接口锁定
- ❌ Verifier Layer 3 (AI review) / Layer 4 (contract)
- ❌ TUI dashboard（用普通 print 输出）
- ❌ 自动 merge

### 验收

**Demo 场景**：

```bash
# 在一个测试 repo 里
cd test-repo
kiro-conduit run-poc \
    --spec specs/add-hello-endpoint.md \
    --target-dir .

# 期望：
# 1. Implementor 在 test-repo 里加一个 /hello 接口
# 2. Verifier 跑 ruff + pytest 通过
# 3. Coordinator 报告 SUCCESS
# 4. test-repo 出现 1 个未提交的 diff
```

**通过条件**：
- 端到端跑通 5 次成功 ≥ 4 次
- Verifier 失败时能正确触发重试
- 进程正常关闭，没有僵尸 `kiro-cli acp`

**预计时长**：5-7 天（含调试）

---

## M1：MVP（多 worker 并行 + 完整机制）

**目标**：真正实现"多 Implementor 并行"，可以跑一个简单的多任务 DAG。

**范围**：完整 6 大模式的最小实现。

### 必做

#### 1. DAG 调度

- [ ] `dag.yaml` 解析 + 校验（无环 / 共享文件清单完整 / 接口锁声明合法）
- [ ] 拓扑排序 + 并行波次识别
- [ ] Coordinator 按波次调度

#### 2. Git Worktree Isolation

- [ ] `kiro_conduit/git/worktree.py`：worktree 创建 / 清理 / 列表
- [ ] 全局 git 锁（防止多 worker 并发跑 fetch / pull）
- [ ] worker 退出 cleanup hook

#### 3. 多 Implementor 并行

- [ ] Worker pool（asyncio + 信号量限并发数）
- [ ] 每个 worker 自己的 ACP 子进程 + 自己的 worktree
- [ ] Coordinator 监听多个 worker 的状态聚合

#### 4. 共享文件锁

- [ ] `kiro_conduit/locks/manager.py`：文件锁 / 三种 policy
- [ ] Implementor 改共享文件前的 hook（写入前抢锁）
- [ ] 死锁检测（超时自动释放）

#### 5. 接口锁定（stub-first）

- [ ] dag.yaml 中的 `interface_lock` 字段解析
- [ ] Coordinator 调度：先派 stub task，stub merge 后才启动并行 task
- [ ] Verifier Layer 4：契约校验

#### 6. Verifier 完整流水线

- [ ] Layer 1：静态（ruff / mypy）
- [ ] Layer 2：动态（pytest）
- [ ] Layer 3：AI 语义 review（用 Kiro CLI 跑 review prompt）
- [ ] Layer 4：契约校验
- [ ] 短路逻辑（前面挂了不走后面）

#### 7. 串行 Merge

- [ ] `kiro_conduit/merge/orchestrator.py`
- [ ] rebase onto main → merge --no-ff
- [ ] 文本冲突暂停 + 终端提示用户
- [ ] merge 后跑集成测试 + 失败回滚

#### 8. TUI Dashboard

- [ ] textual / rich live 选型（开放问题 Q5）
- [ ] DAG 进度图
- [ ] 每个 worker 当前状态（任务 / 工具调用 / 重试次数）
- [ ] 共享文件锁状态
- [ ] token 消耗 + 时间累积

#### 9. BYOA 模型路由

- [ ] `config/models.yaml`：角色 → 模型映射
- [ ] CLI flag 覆盖配置
- [ ] 启动 `kiro-cli acp --model X` 时正确传递

### 验收

**Demo 场景**：

构造一个简单的人工 spec，含 1 个 stub-first 接口锁 + 3 个并行任务：

```yaml
# example/dag.yaml
phases:
  - name: setup
    type: serial
    tasks: [pkg-stub]
  - name: parallel
    type: parallel
    tasks: [pkg-a, pkg-b, pkg-c]
    interface_lock:
      - file: src/lib.py
        owner: pkg-stub
        consumers: [pkg-a, pkg-b, pkg-c]
        mode: stub-first
```

```bash
kiro-conduit run --workspace example/
```

**通过条件**：
- pkg-stub 跑完后 3 个 task 真正并行启动
- 3 个 task 不会互相覆盖文件
- 串行 merge 全部成功
- TUI dashboard 全程可见各 worker 状态
- 总耗时比串行模式快 ≥ 40%

**预计时长**：10-14 天

---

## M2：实战检验（真实大型项目剩余 11 PR）

**目标**：用真实大 spec 跑通，证明 MVP 对真实场景有效。

**范围**：拿真实项目的 `master-plan.md` 当输入，跑完剩余 11 个 PR。

### 必做

- [ ] 实战测试：跑 Phase B（4 PR 并行）
  - 必须能成功完成至少 3/4 个 PR
  - 不能造成目标 repo 损坏
- [ ] 实战测试：跑 Phase C（3 PR 并行）
- [ ] 实战测试：完整 11 PR 端到端
- [ ] 性能优化（基于实战数据）
  - 共享文件锁等待时长记录
  - token 消耗记录
  - 内存峰值记录
- [ ] 故障场景演练
  - kiro-cli 进程意外退出 → 自动恢复
  - 网络问题导致 LLM 调用失败 → 退避重试
  - 磁盘满 → 提前告警
- [ ] 跨仓库支持（主仓库 + 二级仓库同时改）
  - workspace 配置格式定型
  - 多 repo 各自 worktree

### 验收

**核心指标**（对照 PRD §5）：

| 指标 | M2 目标 |
|------|---------|
| Phase B 4 PR 并行成功率 | ≥ 75% |
| 完整 11 PR 跑通 | ≥ 1 次成功 |
| 节省时间比 vs 串行 Kiro | ≥ 50% |
| 共享文件冲突自动检测命中率 | ≥ 80% |
| Verifier 误报率 | ≤ 20% |
| 5 worker 内存峰值 | ≤ 8 GB |

**预计时长**：5-7 天（多数时间在调试真实 spec 暴露的边界 case）

---

## M3：开源发布

**目标**：把项目推向社区，建立反馈循环。

### 必做

- [ ] README 完善（gif demo / 安装 / 上手 5 分钟）
- [ ] 至少 2 个开箱即用的示例 spec
  - 简单示例（学 demo 用）
  - 复杂示例（10+ PR、跨仓库规模）
- [ ] 完整的 user guide（`docs/guide/`）
- [ ] CONTRIBUTING.md
- [ ] CI（pytest + lint，运行在 GitHub Actions）
- [ ] PyPI 发布（`pip install kiro-conduit`）
- [ ] 公开博客 / 视频
  - 起源故事："我开 5 个分支两天没合上代码，所以做了 kiro-conduit"
  - 技术解读：CIV 模式 + 6 大并行编排实践
  - Demo：录屏跑通真实 spec

### 不做

- ❌ 文档站点（GitHub Pages）—— 等有真用户再说
- ❌ Discord / Slack 社区 —— 用 GitHub Discussions 起步

### 验收

- 公开发布后 1 个月内：
  - GitHub stars ≥ 50
  - 至少 1 位非作者贡献者提 issue 或 PR
  - 至少 1 位非作者用过本工具

**预计时长**：3-5 天

---

## M4+（按需，不预设时间）

下面这些**只在收到真实需求后才做**：

| 想法 | 触发条件 |
|------|---------|
| LLM 辅助拆 spec → DAG 草稿 | 用户反复抱怨"手写 dag.yaml 很烦" |
| 跨机器分布式（多 worker 跑在不同机器） | 单机 worker 数 > 10 |
| 支持 Cursor / Claude Code | Kiro 用户群增长慢，扩生态 |
| Web UI dashboard | 团队场景出现 |
| Spec 变更广播（运行中的 worker 跟随更新） | 真有人需要 |
| 共享 build 缓存 / Docker 池 | 用户抱怨 worktree 装依赖太慢 |
| GitHub Actions 集成 | 用户要求 CI 里跑 |

不要提前做。每加一个特性就增加维护成本。

---

## 跟踪进度

每个里程碑完成后，在本文件更新：

- 状态（pending / in-progress / done）
- 实际耗时
- 关键学习 / 偏离原计划的地方

---

## 当前状态

**M0 PoC**：⚪ 待启动（项目骨架刚搭好，文档已就绪）

下一步：写 `pyproject.toml` + 实现 `kiro_conduit/acp/client.py`。
