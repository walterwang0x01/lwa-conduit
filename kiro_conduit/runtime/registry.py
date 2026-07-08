"""Runtime registry / capability discovery."""

from __future__ import annotations

from dataclasses import dataclass

from kiro_conduit.runtime.types import RuntimeConfig


@dataclass(frozen=True, slots=True)
class RuntimeRegistryEntry:
    runtime: RuntimeConfig
    available: bool
    models: list[str]
    default_model: str | None = None
