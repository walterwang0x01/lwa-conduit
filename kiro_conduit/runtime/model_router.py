"""Kiro / Cursor runtime 的模型路由。"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import replace

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


def resolve_runtime_for_prompt(runtime: RuntimeConfig, prompt: str) -> RuntimeConfig:
    if runtime.kind == "cursor-agent-cli":
        return replace(runtime, model=runtime.model or "Auto")
    if runtime.model:
        return runtime

    models, default_model = list_kiro_models(runtime.bin)
    if not models:
        return runtime

    score = _complexity_score(prompt)
    if score >= 7:
        chosen = (
            _pick_first(models, ["claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.6"])
            or _pick_first(models, ["claude-sonnet-5", "claude-sonnet-4.6"])
            or default_model
        )
    elif score >= 4:
        chosen = (
            _pick_first(models, ["claude-sonnet-5", "claude-sonnet-4.6", "claude-sonnet-4.5"])
            or _pick_first(models, ["claude-opus-4.8", "claude-opus-4.7"])
            or default_model
        )
    else:
        chosen = (
            _pick_first(
                models,
                ["claude-sonnet-4.6", "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"],
            )
            or _pick_first(models, ["claude-sonnet-5", "minimax-m2.5", "deepseek-3.2"])
            or default_model
        )
    return replace(runtime, model=chosen or runtime.model)
