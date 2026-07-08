# Runtime Routing

`kiro-conduit` 现在支持两层路由：

1. CLI 路由：`cursor-agent-cli` 或 `kiro-cli-acp`
2. Kiro 模型路由：根据实时可用模型和任务复杂度自动选模

## 设计目标

- 简单任务优先低成本
- 复杂任务优先高成功率
- 不假定本机一定有某个模型
- 把实际命中的 runtime / model 打到日志里

## 推荐生产策略

- implementor：优先 `cursor-agent-cli`
- reviewer：优先 `kiro-cli-acp`
- planner：根据任务大小决定，默认建议 `kiro-cli-acp`

## Kiro tier

- `fast`: 更偏速度 / 成本
- `balanced`: 默认档
- `strong`: 更偏 Sonnet 强能力
- `max`: 更偏 Opus 上限能力

## 调参建议

- 如果你更在意成本：把 `simple` / `medium` 往 `fast`、`balanced` 调
- 如果你更在意成功率：把 `medium` / `hard` 往 `strong`、`max` 调
- 如果复杂任务太早升级：提高 `--kiro-medium-threshold` / `--kiro-hard-threshold`
