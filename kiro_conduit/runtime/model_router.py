"""Kiro / Cursor runtime 的模型路由。"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import replace
from shutil import which

from kiro_conduit.runtime.registry import RuntimeRegistryEntry
from kiro_conduit.runtime.types import RuntimeConfig

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[ -/]*[0-9?])"
)
_CACHE_TTL_SECONDS = 300.0
_MODEL_CACHE: dict[str, tuple[float, list[str], str | None]] = {}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _complexity_score(prompt: str) -> int:
    text = prompt.strip()
    lower = text.lower()
    score = 0
    if len(text) > 800:
        score += 2
    if text.count("\n") >= 6:
        score += 1
    if re.search(r"(先|然后|最后|1\.|2\.|3\.|step|steps)", text, re.IGNORECASE):
        score += 2
    if re.search(r"```|monorepo|全库|跨仓库|重构|架构|review|多文件|并发|dag|workflow", lower):
        score += 3
    return score


def _pick_first(models: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in models:
            return candidate
    return None


def _candidates_for_tier(tier_profile: str, fallback: list[str]) -> list[str]:
    if tier_profile == "max":
        return ["claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.6", *fallback]
    if tier_profile == "strong":
        return ["claude-sonnet-5", "claude-sonnet-4.6", "claude-sonnet-4.5", *fallback]
    if tier_profile == "fast":
        return ["claude-haiku-4.5", "deepseek-3.2", "minimax-m2.5", *fallback]
    return ["claude-sonnet-4.6", "claude-sonnet-4.5", "claude-sonnet-4", *fallback]


def list_kiro_models(bin_path: str) -> tuple[list[str], str | None]:
    now = time.time()
    cached = _MODEL_CACHE.get(bin_path)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1], cached[2]

    try:
        proc = subprocess.run(
            [bin_path, "chat", "--list-models", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - best effort fallback
        logger.warning("[runtime] list-models failed: %s", exc)
        return [], None

    if proc.returncode != 0:
        logger.warning("[runtime] list-models exit=%s", proc.returncode)
        return [], None

    raw = _strip_ansi((proc.stdout or proc.stderr or "").strip())
    if not raw:
        return [], None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("[runtime] list-models parse failed: %s", exc)
        return [], None

    items = parsed.get("models") or []
    models = [str(item.get("model_name")) for item in items if item.get("model_name")]
    default_model = parsed.get("default_model")
    default_value = str(default_model) if default_model else None
    _MODEL_CACHE[bin_path] = (now, models, default_value)
    return models, default_value


def discover_runtime_registry(runtime: RuntimeConfig) -> RuntimeRegistryEntry:
    available = which(runtime.bin) is not None
    if not available:
        return RuntimeRegistryEntry(runtime=runtime, available=False, models=[])
    models: list[str] = []
    default_model: str | None = None
    if runtime.kind == "kiro-cli-acp":
        models, default_model = list_kiro_models(runtime.bin)
    return RuntimeRegistryEntry(
        runtime=runtime, available=True, models=models, default_model=default_model
    )


def resolve_runtime_for_prompt(
    runtime: RuntimeConfig, prompt: str, *, role: str = "generic"
) -> RuntimeConfig:
    score = _complexity_score(prompt)
    if runtime.kind == "cursor-agent-cli":
        chosen = replace(runtime, model=runtime.model or "Auto")
        logger.info(
            "[runtime] role=%s kind=%s model=%s score=%s reason=cursor-fixed-auto",
            role,
            chosen.kind,
            chosen.model,
            score,
        )
        return chosen
    if runtime.model:
        logger.info(
            "[runtime] role=%s kind=%s model=%s score=%s reason=kiro-fixed-profile",
            role,
            runtime.kind,
            runtime.model,
            score,
        )
        return runtime

    registry = discover_runtime_registry(runtime)
    models = registry.models
    if not models:
        logger.info(
            "[runtime] role=%s kind=%s model=%s score=%s reason=kiro-smart-no-list",
            role,
            runtime.kind,
            runtime.model,
            score,
        )
        return runtime

    if score >= runtime.hard_threshold:
        tier = "hard"
        chosen = (
            _pick_first(
                models,
                _candidates_for_tier(runtime.hard_tier, ["claude-sonnet-5", "claude-sonnet-4.6"]),
            )
            or registry.default_model
        )
    elif score >= runtime.medium_threshold:
        tier = "medium"
        chosen = (
            _pick_first(
                models,
                _candidates_for_tier(runtime.medium_tier, ["claude-opus-4.8", "claude-opus-4.7"]),
            )
            or registry.default_model
        )
    else:
        tier = "simple"
        chosen = (
            _pick_first(models, _candidates_for_tier(runtime.simple_tier, ["claude-sonnet-5"]))
            or registry.default_model
        )
    resolved = replace(runtime, model=chosen or runtime.model)
    logger.info(
        "[runtime] role=%s kind=%s model=%s score=%s tier=%s available_models=%s reason=kiro-smart",
        role,
        resolved.kind,
        resolved.model,
        score,
        tier,
        len(models),
    )
    return resolved
