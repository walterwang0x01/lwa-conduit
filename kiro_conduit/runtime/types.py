"""多 Agent CLI 运行时类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RuntimeKind = Literal["kiro-cli-acp", "cursor-agent-cli"]


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Agent CLI 运行时配置（替代裸 kiro_cli_path）。"""

    kind: RuntimeKind = "kiro-cli-acp"
    bin: str = "kiro-cli"
    model: str | None = None
    agent: str | None = None
    force: bool = True
    prompt_timeout: float = 600.0
    idle_timeout: float = 300.0

    @classmethod
    def from_cli(
        cls,
        *,
        kiro_cli: str = "kiro-cli",
        runtime_kind: RuntimeKind = "kiro-cli-acp",
        model: str | None = None,
        timeout: float = 600.0,
        force: bool = True,
    ) -> RuntimeConfig:
        if runtime_kind == "cursor-agent-cli":
            agent_bin = kiro_cli if kiro_cli != "kiro-cli" else "agent"
            return cls(
                kind="cursor-agent-cli",
                bin=agent_bin,
                model=model,
                force=force,
                prompt_timeout=timeout,
            )
        return cls(
            kind="kiro-cli-acp",
            bin=kiro_cli,
            model=model,
            prompt_timeout=timeout,
        )
