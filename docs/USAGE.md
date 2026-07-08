# 使用指南：`kiro-conduit run`

把一份 workspace（含 `dag.yaml`）跑成完整流程：按 DAG 波次并行派给 Kiro 写代码 →
分层验证 → 默认产出可 review 的分支（或 `--merge` 合并）。

如果你还不清楚它在整套多 CLI 体系里的角色，先读 [`SYSTEM_OVERVIEW.md`](./SYSTEM_OVERVIEW.md)。

## 安装

```bash
pip install -e '.[dev]'        # 开发安装（仓库内）
# 或 pipx install kiro-conduit  （PyPI 发布后）
```

前提：已装并登录 [Kiro CLI](https://github.com/kirodotdev/Kiro)（`kiro-cli` 在 PATH 上）。

## 用在你自己的项目上（最短用法）

```bash
pipx install kiro-conduit              # 全局隔离安装，kiro-conduit 进 PATH
cd my-project                          # 你的 git 仓库
kiro-conduit run \
  --workspace ./my-spec-ws \           # 含 dag.yaml + specs 的目录
  --base-repo . \                      # 代码写进当前仓库（spec 与代码同仓库时可省略）
  --venv .venv                         # 用本项目 venv 跑验证（见下）
```

**唯一必填的是 `--workspace`**，其余都有默认值（`--base-repo` 默认 = workspace 目录、
`--base-branch` 默认 = 仓库当前分支、默认 review 模式不合并）。

> ⚠️ **一个必须理解的点**：verifier 跑的是**你仓库自己的** acceptance 命令（`pytest` /
> `ruff` 等），所以它得在**你仓库的环境**里跑。两种方式二选一：
> - 跑之前 `source .venv/bin/activate`（最简单），或
> - 用 `--venv .venv` 显式指定 —— kiro-conduit 会把 `.venv/bin` 前置到 PATH，
>   verifier 和 kiro-cli 都用这个 venv 的工具，不必事先激活。

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
| `--venv <dir>` | 把该 venv 的 `bin/` 前置到 PATH，让验证（pytest/lint）和 kiro-cli 用你项目的工具（默认继承当前 PATH） |
| `--review` | 合并后对**组装好的集成结果**起一个 kiro-cli，对照 spec 审整条 diff，出 `.kiro-conduit/review.md`（抓测试发现不了的 spec 漂移）。默认关 |
| `--review-tasks` | 【较贵】在执行期对**每个 task** 也跑语义审（对照各自 spec，超时 600s）；`--review` 只审整体集成。默认关 |
| `--review-model <id>` | 语义评审用的模型（默认 Kiro 默认模型） |
| `--sandbox` | 【实验】用 OS 沙箱（macOS Seatbelt / Linux bwrap）把 kiro-cli 的**文件写入限制在该 task 的 worktree**，读取/网络放开（不破坏登录）；无对应 OS 工具时自动跳过。默认关 |
| `--merge` | 合并通过的 task 分支到 `kiro-conduit/integration`（默认不合，只产出分支供 review）。**部分任务失败时，仍会把已通过的合进 integration 并报告失败项**，不会因一个失败丢掉全部成果 |
| `--resume` | 从上次 run-state 续跑，已通过的 task 不重跑 |
| `--dashboard` | rich 实时 TUI（wave / worker / 锁 / merge 状态） |
| `--diagnose` | merge 冲突时产出结构化诊断（冲突文件 + 内容） |
| `--max-concurrency N` | 同波次最大并发（默认 4） |
| `--max-attempts N` | 单 task 失败重试上限（默认 3） |
| `--kiro-cli <path>` | kiro-cli 可执行文件路径（默认 `kiro-cli`） |
| `--kiro-simple-tier <tier>` | Kiro 简单任务优先 tier：`fast` / `balanced` / `strong` / `max` |
| `--kiro-medium-tier <tier>` | Kiro 中等任务优先 tier |
| `--kiro-hard-tier <tier>` | Kiro 复杂任务优先 tier |
| `--kiro-medium-threshold N` | 进入中等复杂度路由的阈值（默认 4） |
| `--kiro-hard-threshold N` | 进入高复杂度路由的阈值（默认 7） |
| `--adaptive-mode <mode>` | 自适应：`off` / `suggest`（默认）/ `apply-safe` / `apply-aggressive`；按角色桶（implementor/planner/reviewer）生效 |
| `--implementor-runtime-kind` / `--reviewer-runtime-kind` / `--planner-runtime-kind` | 角色级 runtime 覆盖 |
| `--implementor-bin` / `--reviewer-bin` / `--planner-bin` | 角色级二进制覆盖 |

`plan` 子命令同样支持 `--adaptive-mode` 与 planner runtime 相关参数。

查看历史指标：

```bash
kiro-conduit report --base-repo .
```

## 成本优先的多模型路由

当 runtime 是 `kiro-cli-acp` 时，`kiro-conduit` 会先根据 prompt 复杂度打分，再从
`kiro-cli chat --list-models --format json` 的实时结果中选模型，而不是硬编码假定模型名。

指标按角色分桶（`implementor` / `planner` / `reviewer`），自适应用多目标分数
（成功率 + 耗时 + 改动规模 + 成本），而不是只看成功率。reviewer 的审查结论
（`verdict_pass`）与 runtime 执行成败（`execution_ok`）分开统计。

推荐起步策略：

```bash
kiro-conduit run \
  --workspace my-workspace/ \
  --runtime-kind cursor-agent-cli \
  --kiro-cli agent \
  --reviewer-runtime-kind kiro-cli-acp \
  --reviewer-bin kiro-cli \
  --adaptive-mode suggest \
  --kiro-simple-tier balanced \
  --kiro-medium-tier strong \
  --kiro-hard-tier max
```

这个组合适合“实现便宜优先，评审能力优先”：

- implementor：默认更偏低成本 / 高吞吐
- reviewer：默认更偏强模型
- planner：如需更稳，也可以单独切到 `kiro-cli-acp`

完整生产说明见 [`runtime-routing.md`](./runtime-routing.md)。

## 全局约定注入（`conventions`）

跨任务的共识（如"全后端统一异步 `AsyncSession`""错误返回统一用 `AppError`""测试一律
用 pytest"）写在 `dag.yaml` 顶层 `conventions`，会被**注入每一个任务的 prompt 头部**。
各任务由独立 Kiro 实例执行、彼此看不见对方的选择——没有全局约定时，它们会各自做出
局部合理但全局不一致的决定（A 用异步、B 用同步），只在合并时才暴露。`conventions`
从源头消除这类分裂，等价于团队的「架构约定 / 风格指南」。

```yaml
conventions: |
  - 所有数据库访问统一用异步 SQLAlchemy（AsyncSession），service 一律 async def；
  - 错误统一抛 AppError；响应统一用 app/core/response 的 ok/fail。
phases: [...]
tasks: {...}
```

> 配合「共享基建文件归 foundation 任务独家所有」（见下）效果最好：约定定行为，
> 单一 owner 定文件，两者一起把跨任务一致性钉死。

## 每个 worktree 的准备（`setup`）

每个 task 在自己的 git worktree 里跑。若项目需要 per-worktree 准备（装依赖、生成
配置、起本地服务），在 `dag.yaml` 顶层声明 `setup` 命令——**每个 worktree 创建后、
agent 动手前**在该 worktree 目录里执行一次，并能读到下面的隔离环境变量：

```yaml
setup: uv sync --frozen        # 或 npm ci / pip install -e . / bash scripts/setup.sh
phases: [...]
tasks: {...}
```

setup 非 0 退出或超时（默认 900s）→ 该 task 直接判失败。`--venv` 是这件事的轻量
特例（只前置一个已建好的 venv 到 PATH，不跑命令）；两者可单用也可叠加。

### 拷贝本地文件进 worktree（`copy_files`）

worktree 只含 git 跟踪的文件，**gitignored 的本地文件（如 `.env`）不会进去**，
但测试/应用常需要它们。用 `copy_files` 声明要拷进每个 worktree 的本地文件
（相对 base repo 解析，源缺失则跳过）：

```yaml
copy_files: ['.env', 'config/local.yaml']
```

在依赖合入之后、`setup` 之前执行，所以 `setup` 能用到拷进来的文件。

### 自动修复 / 格式化（`format`）

每个 task 验证**之前**，在它的 worktree 里跑一次自动修复/格式化命令，把机械的
lint/格式问题先修掉——agent 只会在真问题（测试/类型/逻辑）上被卡，而不是被
吹毛求疵的风格规则反复打回（实测 LLM 常栽在这类琐碎 lint 上）：

```yaml
format: ruff check --fix . && ruff format .   # 或 eslint --fix / prettier -w
```

### 集成结果全量验证（`integration_check`）

各 task 只验证自己那一块；要确认"拼起来还能跑"，在 `dag.yaml` 顶层声明
`integration_check` 命令——`--merge` 组装出集成分支后，在它的独立 worktree 里
（已应用 `copy_files`）跑一次，报告通过与否，失败则整体退出码非 0：

```yaml
integration_check: pytest -q
```

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
