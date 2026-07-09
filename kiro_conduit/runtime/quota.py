"""Runtime 配额探测与 fallback（与 Bridge quota.ts 契约对齐）。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Literal

QuotaState = Literal["healthy", "depleted", "unknown", "error"]

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SEC = 600.0


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    runtime_kind: str
    state: QuotaState
    detail: str
    remaining_ratio: float | None = None


def _quota_payload(status: QuotaStatus) -> dict:
    return {
        "runtime_kind": status.runtime_kind,
        "state": status.state,
        "detail": status.detail,
        "remaining_ratio": status.remaining_ratio,
    }


def _cache_status(cache_key: str, status: QuotaStatus) -> None:
    _CACHE[cache_key] = (time.time() + _CACHE_TTL_SEC, _quota_payload(status))


def _load_overrides() -> dict[str, QuotaState]:
    raw = os.environ.get("KIRO_CONDUIT_QUOTA_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, QuotaState] = {}
    for key, value in data.items():
        if value in {"healthy", "depleted", "unknown", "error"}:
            out[str(key)] = value  # type: ignore[assignment]
    return out


def fallback_kinds_for_bucket(bucket: str) -> list[str]:
    if bucket in {"planner", "reviewer"}:
        return ["kiro-cli-acp", "gemini-cli", "cursor-agent-cli"]
    return ["cursor-agent-cli", "gemini-cli", "kiro-cli-acp"]


def probe_runtime_kind(runtime_kind: str, *, month_usage: int | None = None) -> QuotaStatus:
    cache_key = runtime_kind
    cached = _CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        payload = cached[1]
        return QuotaStatus(
            runtime_kind=payload["runtime_kind"],
            state=payload["state"],
            detail=payload["detail"],
            remaining_ratio=payload.get("remaining_ratio"),
        )

    overrides = _load_overrides()
    if runtime_kind in overrides:
        status = QuotaStatus(
            runtime_kind=runtime_kind,
            state=overrides[runtime_kind],
            detail="env-override",
        )
        _cache_status(cache_key, status)
        return status

    # Kiro free tier proxy when KIRO_CONDUIT_KIRO_MONTHLY_LIMIT is set
    kiro_limit = os.environ.get("KIRO_CONDUIT_KIRO_MONTHLY_LIMIT")
    if runtime_kind == "kiro-cli-acp" and kiro_limit and month_usage is not None:
        try:
            limit = int(kiro_limit)
        except ValueError:
            limit = 0
        if limit > 0 and month_usage >= limit:
            status = QuotaStatus(
                runtime_kind=runtime_kind,
                state="depleted",
                detail=f"monthly usage {month_usage}/{limit}",
                remaining_ratio=0.0,
            )
            _cache_status(cache_key, status)
            return status

    status = QuotaStatus(runtime_kind=runtime_kind, state="unknown", detail="no probe source")
    _cache_status(cache_key, status)
    return status


def is_quota_blocked(status: QuotaStatus) -> bool:
    return status.state in {"depleted", "error"}


def pick_first_available_kind(
    kinds: list[str],
    *,
    month_usage_by_kind: dict[str, int] | None = None,
) -> str | None:
    usage = month_usage_by_kind or {}
    for kind in kinds:
        status = probe_runtime_kind(kind, month_usage=usage.get(kind))
        if not is_quota_blocked(status):
            return kind
    return None
