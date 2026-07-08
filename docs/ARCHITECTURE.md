# 架构：kiro-conduit

> 受众：项目实现者 / 想读懂代码的人 / 想改架构的人
>
> 作用：回答"系统怎么搭、为什么这么搭、写代码时要遵守哪些边界"
>
> 先看整体定位与对外说明：[`SYSTEM_OVERVIEW.md`](./SYSTEM_OVERVIEW.md)；先看生产调参：[`runtime-routing.md`](./runtime-routing.md)

---

## 1. 一图看懂

```
┌─────────────────────────────────────────────────────────────────────┐
│                   Living Spec Layer (Markdown + YAML)                │
│   - master-plan.md（人写）                                            │
│   - dag.yaml（人写或半自动）                                          │
│   - 包级 spec.md（人写）                                              │
│   ↕ 读写：所有 agent 从这里读，写回更新进度                           │
└─────────────────────────────────────────────────────────────────────┘
                                 ↕
┌─────────────────────────────────────────────────────────────────────┐
│                   Coordinator (1 个，长生命周期)                     │
│  职责：                                                               │
│   - 解析 spec → 验证 DAG（拓扑、共享文件、接口锁）                    │
│   - 调度任务（拓扑序，能并行就并行）                                  │
│   - 监听 Verifier 结果，决定重试 / 替换 / 升级人工                    │
│   - 触发串行 merge                                                    │
│  实现：1 个 kiro-cli acp 子进程 + 强模型（claude-opus / gpt-5）       │
└─────────────────────────────────────────────────────────────────────┘
                                 ↕
┌─────────────────────────────────────────────────────────────────────┐
│              Implementor Pool (N 个，短生命周期，并行)               │
│                                                                      │
│  每个 Implementor =                                                  │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ git worktree (自己的工作目录)                                │   │
│   │ kiro-cli acp 子进程 (自己的 cwd + session)                   │   │
│   │ 任务上下文：                                                 │   │
│   │   - 瘦身后的 spec 视图（.kiro-conduit/task.md）              │   │
│   │   - 文件归属清单（哪些归我）                                 │   │
│   │   - 共享文件锁查询接口                                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  实现：每任务 spawn 一个 kiro-cli acp + 均衡模型（sonnet）           │
└─────────────────────────────────────────────────────────────────────┘
                                 ↕
┌─────────────────────────────────────────────────────────────────────┐
│              Verifier (每任务 1 个，短生命周期)                      │
│  4 层流水线（短路：前面挂了不走后面）                                 │
│   1. 静态检查 (ruff / mypy / eslint)         ← 秒级                  │
│   2. 动态测试 (pytest / jest)                 ← 分钟级               │
│   3. AI 语义 review (Kiro CLI + 分析模型)    ← LLM 调用              │
│   4. 接口契约校验 (针对 stub-first 阶段)     ← 字符串/AST 比对       │
│                                                                      │
│  PASS → 通知 Coordinator 进入下一节点                                │
│  FAIL → 把 feedback 喂回 Implementor 重试（最多 3 次，超出转人工）   │
└─────────────────────────────────────────────────────────────────────┘
                                 ↕
┌─────────────────────────────────────────────────────────────────────┐
│                   Merge Orchestrator                                 │
│  - 按 DAG 拓扑序，串行处理每个完成的分支                              │
│  - rebase onto main → merge --no-ff                                  │
│  - 文本冲突 → 暂停，推送终端通知人工                                  │
│  - merge 后跑集成测试，挂了回滚                                       │
└─────────────────────────────────────────────────────────────────────┘
                                 ↕
┌─────────────────────────────────────────────────────────────────────┐
│                   Dashboard (终端 TUI)                               │
│  - DAG 进度图（每个节点状态）                                         │
│  - 每个 Implementor 当前在做什么（实施 / review / 修复 / commit）    │
│  - 共享文件锁状态                                                     │
│  - 重试次数 + token 消耗                                              │
└─────────────────────────────────────────────────────────────────────┘
```

## 2. CIV 三角色（核心，不可省）

CIV = **Coordinator / Implementor / Verifier**。这是 Augment Cosmos、VeriMAP、Anthropic Claude Code 都在用的范式。**任何想缩减成两角色的设计都要被打回**。

### 2.1 为什么三角色不能缩减

| 缩减方案 | 失败模式 |
|---------|---------|
| 只 Coordinator + Implementor | Implementor 自己说自己做对了，幻觉传染下游 |
| 只 Implementor + Verifier | 没人做 DAG 调度，重复实现、抢共享文件 |
| 单 agent 全干 | LLM 同时做规划+执行+审查会偷懒，质量崩 |

VeriMAP 论文实测：**Coordinator 质量决定整个系统上限**，Verifier 质量决定下限。

### 2.2 Coordinator

- **生命周期**：长（整个 spec 跑完才退）
- **进程**：1 个 `kiro-cli acp`
- **模型**：强推理（claude-opus / gpt-5 / o1）
- **输入**：spec.md + dag.yaml
- **输出**：派发指令到 Implementor，监听 Verifier 反馈
- **关键约束**：
  - 不直接写代码（read-only）
  - 不 merge（merge 是 Merge Orchestrator 的事）
  - DAG 验证失败必须停下问人（不要乱猜）

### 2.3 Implementor

- **生命周期**：短（一个任务一个进程，做完即弃）
- **进程**：N 个 `kiro-cli acp`，每个自己的 cwd
- **模型**：均衡（claude-sonnet / gpt-4）
- **输入**：瘦身的 spec 视图 + 文件归属清单 + 共享文件锁状态
- **输出**：worktree 里的代码改动 + commit
- **关键约束**：
  - **绝对不能改自己 cwd 之外的文件**
  - 改共享文件必须先抢锁（找不到锁直接退）
  - 不跑 Verifier（自己不能审自己）
  - 重试上限 3 次（VeriMAP 默认）

### 2.4 Verifier

- **生命周期**：短（一个任务一个 Verifier）
- **进程**：1 个 `kiro-cli acp`（或直接 LLM API，看 ADR-003 实测）
- **模型**：分析（claude-sonnet 分析模式 / o1-mini）
- **输入**：Implementor 的 git diff + 任务 spec 视图
- **输出**：结构化 JSON `{pass: bool, feedback: str, retry_hint: str}`
- **关键约束**：
  - **必须按 4 层流水线顺序**：静态 → 动态 → 语义 → 契约
  - Layer 1/2 用脚本不用 LLM（节省 token）
  - 反馈必须结构化（不能是自由散文）
  - 不能改代码（read-only）

## 3. 6 大模式落地清单

每个模式都说**怎么实现 + 关键代码点 + 反面案例**。

### 3.1 模式 1：Spec-Driven Decomposition

**怎么做**：

输入文件结构：
```
your-project/
├── master-plan.md        # 人写：phase / 包列表 / 共享文件清单
├── dag.yaml              # 人写或半自动：DAG 形式化
└── specs/
    ├── pkg-base.md       # 每个包一份独立 spec
    ├── pkg-auth.md
    └── ...
```

`dag.yaml` 格式（示例：一个 web 应用，含基础设施、3 个并行业务模块）：

```yaml
phases:
  - name: A
    type: serial
    tasks: [pkg-base, pkg-shared-types]
  - name: B
    type: parallel
    tasks: [pkg-auth, pkg-payment, pkg-admin]
    interface_lock:
      - file: src/lib/event_bus.py
        owner: pkg-shared-types
        consumers: [pkg-auth, pkg-payment, pkg-admin]
        mode: stub-first

tasks:
  pkg-shared-types:
    spec: specs/pkg-shared-types.md
    depends_on: []
    files_owned: [src/lib/event_bus.py, src/lib/types.py]
    shared_files_to_modify: []
    max_lines: 800
    max_files: 12
  pkg-auth:
    spec: specs/pkg-auth.md
    depends_on: [pkg-shared-types]
    files_owned: [src/auth/*]
    shared_files_to_modify:
      - src/constants.py
      - src/db_init.py
    max_lines: 800
    max_files: 12

shared_files:
  - path: src/constants.py
    policy: append-only       # 只允许追加，不允许中段修改
  - path: src/db_init.py
    policy: single-writer     # 同时只能一个 worker 改
  - path: src/main.py
    policy: coordinator-only  # 只有 Coordinator 能改
```

**关键代码点**：
- `kiro_conduit/spec/parser.py` —— 解析 master-plan + dag.yaml
- `kiro_conduit/spec/validator.py` —— 校验 DAG 无环、共享文件清单完整、接口锁定声明合法

**反面案例**：
- ❌ 让 LLM 自动从 markdown 推断 DAG（不可控）
- ❌ 共享文件清单留空（必然冲突）
- ✅ 强制要求 dag.yaml 显式声明所有共享文件

### 3.2 模式 2：Git Worktree Isolation

**怎么做**：

```python
def create_worktree(task_id: str, base_branch: str = "main") -> Path:
    worktree_path = Path(".kiro-conduit/worktrees") / task_id
    branch = f"kiro-conduit/{task_id}"
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch, base_branch],
        check=True,
    )
    return worktree_path
```

每个 Implementor 启动 `kiro-cli acp` 时设 cwd 为这个 worktree 路径。

**关键代码点**：
- `kiro_conduit/git/worktree.py` —— worktree 创建 / 清理 / 列表
- 注意：**串行化 git 操作**，多 worker 同时跑 `git fetch` 会损坏 metadata

**坑**：
- 共享 .git 但 working directory 隔离 ✅
- **build 缓存（.venv / node_modules / target）跨 worktree 共享 ❌** —— MVP 不解决，文档里警告即可
- Docker / 数据库 / 缓存跨 worktree 共享 ❌ —— MVP 不解决

**反面案例**：
- ❌ 多 worker 并行 `git fetch / git pull` —— 用全局锁串行化
- ❌ worker 退出不清理 worktree —— 加 cleanup 钩子

### 3.3 模式 3：CIV 三角色

见 §2，不重复。

实现位置：
- `kiro_conduit/roles/coordinator.py`
- `kiro_conduit/roles/implementor.py`
- `kiro_conduit/roles/verifier.py`

通信协议（结构化 JSON over stdin/stdout）：

```python
# Coordinator → Implementor
{
    "type": "task_assigned",
    "task_id": "pkg-auth",
    "worktree": "/path/to/worktree",
    "spec_view": "...",  # 瘦身后的 markdown
    "files_owned": [...],
    "shared_files": [...],
    "interface_locks": [...],
}

# Implementor → Verifier (via Coordinator)
{
    "type": "task_complete",
    "task_id": "pkg-auth",
    "diff": "...",
    "files_changed": [...],
    "tests_added": [...],
}

# Verifier → Coordinator
{
    "type": "verification_result",
    "task_id": "pkg-auth",
    "pass": false,
    "layer_results": {
        "static": {"pass": true, "details": "..."},
        "dynamic": {"pass": false, "details": "test_xxx failed: ..."},
        "semantic": null,  # 未执行（前面挂了）
        "contract": null,
    },
    "feedback": "...",
    "retry_hint": "...",
}
```

### 3.4 模式 4：BYOA Model Routing

**怎么做**：

每个角色启动 `kiro-cli acp` 时通过 `--model` 指定：

```python
ROLE_MODEL_MAP = {
    "coordinator": "claude-opus-4.7",     # 强推理
    "implementor": "claude-sonnet-4.7",   # 均衡
    "verifier": "claude-sonnet-4.7",      # 分析（可换 o1-mini）
}

subprocess.Popen(
    ["kiro-cli", "acp", "--model", ROLE_MODEL_MAP[role]],
    cwd=worktree,
    stdin=PIPE, stdout=PIPE,
)
```

**关键代码点**：
- `kiro_conduit/config/models.yaml` —— 角色 → 模型映射，可被用户覆盖

**反面案例**：
- ❌ 全部用 opus（5-15x token 成本爆炸）
- ❌ Coordinator 用便宜模型（DAG 拆错全盘崩）

### 3.5 模式 5：Multi-Layer Verification

**怎么做**：

```python
class Verifier:
    def verify(self, task: Task, diff: str) -> VerifyResult:
        # Layer 1: 静态（秒级，必须先）
        r1 = self.run_static_checks(task)  # ruff / mypy / eslint
        if not r1.pass_:
            return VerifyResult(pass_=False, layer="static", feedback=r1.errors)

        # Layer 2: 动态（分钟级）
        r2 = self.run_tests(task)  # pytest / jest
        if not r2.pass_:
            return VerifyResult(pass_=False, layer="dynamic", feedback=r2.failures)

        # Layer 3: AI 语义（LLM 调用，最贵）
        r3 = self.run_ai_review(task, diff)
        if not r3.pass_:
            return VerifyResult(pass_=False, layer="semantic", feedback=r3.review)

        # Layer 4: 接口契约（stub-first 阶段才有）
        if task.interface_locks:
            r4 = self.check_contracts(task, diff)
            if not r4.pass_:
                return VerifyResult(pass_=False, layer="contract", feedback=r4.diff)

        return VerifyResult(pass_=True)
```

**关键代码点**：
- `kiro_conduit/verifier/static.py` / `dynamic.py` / `semantic.py` / `contract.py`
- 每个 Layer 独立模块，方便禁用 / 替换

**反面案例**：
- ❌ AI review 在最前（贵且慢，浪费 token）
- ❌ 任意 Layer 挂了继续走（污染后续判断）

### 3.6 模式 6：Sequential Merge

**怎么做**：

```python
def merge_completed_tasks(dag: DAG) -> None:
    for task in dag.topological_order():
        if task.status != "verified":
            continue

        branch = f"kiro-conduit/{task.id}"

        # 1. rebase onto latest main
        run(["git", "checkout", branch])
        run(["git", "fetch", "origin", "main"])
        result = run(["git", "rebase", "origin/main"])
        if result.has_conflict:
            notify_user_for_manual_resolution(task, result)
            wait_for_user_continue()

        # 2. merge into main
        run(["git", "checkout", "main"])
        run(["git", "merge", "--no-ff", branch, "-m", task.commit_message])

        # 3. 集成测试
        if not run_integration_tests():
            run(["git", "reset", "--hard", "HEAD~1"])  # 回滚
            notify_user_for_investigation(task)
            wait_for_user_continue()
```

**关键代码点**：
- `kiro_conduit/merge/orchestrator.py`
- 不要并行 merge，**串行 + rebase 是定论**

**反面案例**：
- ❌ 自动解决冲突（行业共识：不可能可靠）
- ❌ 跳过 rebase 直接 merge（冲突会越积越多）

## 4. 共享文件单一写者机制

这是你的实战经验里最关键的一条。MVP 必须有。

### 4.1 锁的实现

```
.kiro-conduit/
└── locks/
    ├── app__agent_pay__constants.py.lock     # 文件名转义后做锁文件
    └── app__main.py.lock
```

锁文件内容：

```json
{
  "task_id": "pkg-auth",
  "acquired_at": "2026-05-28T10:00:00Z",
  "policy": "single-writer"
}
```

### 4.2 三种 policy

| policy | 行为 |
|--------|------|
| `single-writer` | 同时只能一个任务持有锁，其他任务阻塞 |
| `append-only` | 任意任务可以写，但只允许 `>>` 追加，不允许中段改（用 git diff 校验） |
| `coordinator-only` | 只有 Coordinator 能改，Implementor 把需求写到 spec 里登记 |

### 4.3 Implementor 的写入流程

```python
def write_shared_file(task_id, file_path, content):
    policy = lookup_policy(file_path)
    if policy == "coordinator-only":
        register_pending_change(task_id, file_path, content)
        return  # 由 Coordinator 后续处理

    lock = acquire_lock(file_path, task_id, timeout=60)
    try:
        if policy == "append-only":
            verify_append_only(file_path, content)
        write(file_path, content)
    finally:
        release_lock(lock)
```

**关键代码点**：`kiro_conduit/locks/manager.py`

## 5. 接口锁定（Stub-First）

这是你的另一个实战经验，行业其他工具都没明确做。

### 5.1 工作流

```
DAG 阶段 B (4 task 并行) 启动前：
  1. Coordinator 派 1 个 Implementor 跑"接口包"任务
  2. 这个 Implementor 只写接口 stub（class + method 签名 + docstring，函数体是 pass / raise NotImplementedError）
  3. Verifier 验证接口完整性
  4. 接口 commit 到一个 base 分支
  5. 4 个并行 task 从这个 base 分支起 worktree
  6. 每个 task 都能读到锁定的接口
```

### 5.2 dag.yaml 中的声明

```yaml
phases:
  - name: B
    type: parallel
    interface_lock:
      - file: src/lib/event_bus.py
        owner: pkg-shared-types     # 谁负责定义
        consumers: [pkg-auth, pkg-payment, pkg-admin]
        mode: stub-first       # 先 stub，再并行
```

### 5.3 Verifier Layer 4：契约校验

并行 task 完成后，Verifier 检查它们对接口的实现**没有偷偷修改签名**：

```python
def check_contracts(task, diff):
    for lock in task.interface_locks:
        if lock.file in diff.changed_files:
            sig_before = parse_signatures(read_at_commit(lock.commit, lock.file))
            sig_after = parse_signatures(read_current(lock.file))
            if sig_before != sig_after:
                return ContractViolation(file=lock.file, diff=...)
```

## 6. 模块结构

```
kiro_conduit/
├── __init__.py
├── cli.py                    # 命令行入口
│
├── spec/
│   ├── parser.py             # master-plan + dag.yaml 解析
│   └── validator.py          # DAG 校验
│
├── roles/
│   ├── coordinator.py
│   ├── implementor.py
│   └── verifier.py
│
├── acp/
│   ├── client.py             # ACP JSON-RPC 客户端（可参考 lark-kiro-bridge）
│   └── messages.py           # 消息类型定义
│
├── git/
│   ├── worktree.py
│   └── merge.py
│
├── locks/
│   └── manager.py            # 共享文件锁
│
├── verifier/
│   ├── static.py             # ruff / mypy / eslint
│   ├── dynamic.py            # pytest / jest
│   ├── semantic.py           # AI review
│   └── contract.py           # 接口契约
│
├── dashboard/
│   └── tui.py                # textual TUI
│
└── config/
    ├── models.yaml           # 角色 → 模型映射
    └── defaults.py
```

## 7. 边界与反例（容易踩错的）

| 错误做法 | 正确做法 |
|---------|---------|
| Coordinator 自己写代码 | Coordinator 只调度，read-only |
| Implementor 改 cwd 之外的文件 | 写共享文件必须走锁机制 |
| Verifier 自己改代码修 bug | Verifier read-only，反馈给 Implementor 重试 |
| 自动解决 git 冲突 | 文本冲突暂停让人解 |
| LLM 自动拆 spec 进 DAG | MVP 只接受人写的 dag.yaml |
| 全部任务用 opus | BYOA 路由：Coordinator opus，其他 sonnet |
| 全部 Layer 都用 LLM | 静态/动态在前，LLM 在后 |
| Verifier 反馈用自由文本 | 强制结构化 JSON |
| 并行 merge | 串行 + rebase |

## 8. 性能 / 资源预算（MVP 阶段）

| 维度 | 目标 |
|------|------|
| 单 worker 内存峰值 | ≤ 1.5 GB |
| 5 worker 同时跑 | ≤ 8 GB |
| 单任务 token 消耗 | ≤ 500K |
| 5 任务并行总 token | ≤ 3M（含 Coordinator + Verifier） |
| Coordinator 心跳间隔 | 2 秒 |
| Implementor 心跳超时 | 60 秒（无 ACP 输出视为卡住） |

## 9. 演进路径

| 阶段 | 状态 | 加什么 |
|------|------|--------|
| M0 PoC | 当前 | 1 Coordinator + 1 Implementor + 1 Verifier 跑通最小链路 |
| M1 MVP | — | 多 worker 并行 + 共享文件锁 + 接口锁定 + TUI dashboard |
| M2 实战 | — | 跑通真实大 spec + 性能优化 + token 计量 |
| M3 开源 | — | 文档完善 + 示例 spec + GitHub Action 集成 |
| M4+（按需） | — | LLM 辅助拆 spec / 跨机器分布式 / 多 agent 框架支持 |

详见 [ROADMAP.md](ROADMAP.md)。

## 10. 关键参考资料

- [Augment Code: How to Run a Multi-Agent Coding Workspace (2026)](https://www.augmentcode.com/guides/how-to-run-a-multi-agent-coding-workspace) —— 6 大模式的源头
- [Augment Code: Coordinator-Implementor-Verifier Pattern](https://www.augmentcode.com/guides/coordinator-implementor-verifier) —— CIV 范式详解
- [VeriMAP (EACL 2026)](https://arxiv.org/html/2510.17109) —— DAG-structured 多 agent 编排的学术基础
- [Anthropic: Built Multi-Agent Research System](https://www.anthropic.com/engineering/built-multi-agent-research-system) —— Orchestrator-Worker 模式
- [Agent Client Protocol](https://agentclientprotocol.com/) —— Kiro CLI 用的协议规范
- 真实大型项目的 `master-plan.md` 实战经验 —— 本项目的真实需求来源
