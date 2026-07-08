# 系统总览：kiro-conduit 在多 CLI 体系中的角色

这份文档不讨论实现细节，而是帮助读者快速理解：

- `kiro-conduit` 在整个系统里的位置
- 为什么它现在支持多 CLI / 多角色 / 自适应路由
- 生产上应该怎么使用它

## 定位

`kiro-conduit` 是一个面向大 spec 的并行编排器，不是普通聊天入口。

它的职责是：

1. 接受规划后的 workspace / `dag.yaml`
2. 按角色分配 runtime
3. 并行执行 implementor
4. 对结果做 verifier / reviewer 检查
5. 输出可 review / 可 merge 的结果

## 在整套系统中的位置

- `lark-kiro-bridge` 负责飞书入口与交互式体验
- `kiro-conduit` 负责长任务、并行、角色隔离和编排

如果说 bridge 解决的是“怎么在本机上把 Agent 用起来”，那 conduit 解决的是“怎么把一个大任务拆开并稳定执行完”。

## 为什么现在支持多 CLI

因为不同角色的目标不同：

- planner 更重视稳定和理解能力
- implementor 更重视吞吐和成本
- reviewer 更重视审查质量

所以一个 runtime 很难同时最优。

## 三个核心 bucket

- `planner`
- `implementor`
- `reviewer`

它们的指标、推荐和 adaptive 行为分开学习，避免互相污染。

## reviewer 的特殊点

reviewer 有两个概念：

- runtime 有没有正常跑完：`execution_ok`
- 审查结论是不是 PASS：`verdict_pass`

这两个不能混。发现问题是 reviewer 的工作成果，不是 reviewer 失败。

## 推荐生产用法

```bash
kiro-conduit run \
  --workspace my-workspace/ \
  --runtime-kind cursor-agent-cli \
  --kiro-cli agent \
  --reviewer-runtime-kind kiro-cli-acp \
  --reviewer-bin kiro-cli \
  --adaptive-mode suggest
```

## 推荐阅读顺序

1. `USAGE.md`：先会用
2. `runtime-routing.md`：再会调参
3. `ARCHITECTURE.md`：最后看实现
4. `PRD.md` / `ROADMAP.md`：理解定位和演进方向
