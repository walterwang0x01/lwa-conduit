# Runtime Routing（生产完整版）

`kiro-conduit` 的生产路由是**两层路由 + 角色分桶 + 多目标自适应**：

1. CLI 路由：`cursor-agent-cli` 或 `kiro-cli-acp`
2. Kiro 模型路由：复杂度 + 实时 `--list-models` 选模
3. 自适应：按角色桶学习，综合成功率 / 耗时 / 改动 / 成本

## 设计目标

- 简单实现优先低成本、高吞吐
- 规划 / 审查优先稳定与成功率
- 不假定本机一定有某个模型名
- 角色互不污染（planner ≠ implementor ≠ reviewer）
- 审查结论与 runtime 成败拆开
- 把实际命中的 runtime / model / score 打进日志和 metrics

## 推荐生产角色策略

| Role | 推荐 runtime | 说明 |
|------|--------------|------|
| implementor | `cursor-agent-cli` (`Auto`) | 便宜、快，适合并行落地 |
| planner | `kiro-cli-acp` | 拆 DAG 要稳 |
| reviewer | `kiro-cli-acp` | 语义审查能力优先 |

起步命令：

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

## 任务分桶

| Bucket | 何时写入 |
|--------|----------|
| `implementor` | `run` 每个 task 的最终执行结果 |
| `planner` | `plan` 成功或失败各记一条 |
| `reviewer` | `--review-tasks` 的 SEMANTIC 层；`--review` 的集成审查 |

兼容说明：历史数据若还是 `conduit-run`，implementor 自适应仍会回读它们。

## Kiro tier

| Tier | 意图 |
|------|------|
| `fast` | 速度 / 成本 |
| `balanced` | 默认 |
| `strong` | Sonnet 强能力 |
| `max` | Opus 上限 |

调参：

- 更在意成本：simple / medium → `fast` / `balanced`
- 更在意成功率：medium / hard → `strong` / `max`
- 过早升级：提高 `--kiro-medium-threshold` / `--kiro-hard-threshold`

## Adaptive 模式

| Mode | 行为 |
|------|------|
| `off` | 禁用 |
| `suggest` | 默认；只在 report / 日志里给建议 |
| `apply-safe` | 样本与成功率达阈值后才覆盖 runtime（可选再覆盖 model） |
| `apply-aggressive` | 有推荐就覆盖 |

`run` 与 `plan` 都支持 `--adaptive-mode`，并**按各自角色桶**生效。

## 多目标评分

每个 `(runtime_kind, model)` 行会算综合 `score`，主要看：

- success rate
- avg duration（有则用真流水耗时；否则用重试次数作代理）
- avg files changed / attempts
- cost proxy（cursor 高、opus 低）

`kiro-conduit report` 会按桶打印 score 与推荐。

## Reviewer：执行 vs 审查结论

不要把「审出 FAIL」当成模型失败。

- `execution_ok` / `passed`：审学生父进程是否正常结束（崩溃、超时 = 失败）
- `verdict_pass`：PASS / FAIL 结论（FAIL = 发现了问题，属于有效工作）

自适应只基于 execution；`verdict_pass_rate` 仅用于观测。

## Metrics 文件

路径：`<base-repo>/.kiro-conduit/runtime-metrics.json`

常用命令：

```bash
kiro-conduit report --base-repo .
kiro-conduit plan --spec spec.md --out ws/ --adaptive-mode suggest
kiro-conduit run --workspace ws/ --adaptive-mode apply-safe --review --review-tasks
```

## 生产 rollout 建议

1. 第一阶段：`--adaptive-mode suggest`，先攒样本
2. 第二阶段：implementor / planner / reviewer 分别确认后切 `apply-safe`
3. 第三阶段：仅在你明确知道“该角色已经收敛”时，再考虑 `apply-aggressive`
4. 始终让 reviewer 保留更强 runtime；不要为了省钱把审查也切成最弱模型

跨项目总览见 `lark-kiro-bridge` 的 `docs/runtime-routing-production.md`。

如果你想先看团队/开源读者视角的整体说明，再看本页调参细节，先读 [`SYSTEM_OVERVIEW.md`](./SYSTEM_OVERVIEW.md)。
