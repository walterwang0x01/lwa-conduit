"""跨 runtime 的 session id 编解码。"""

from __future__ import annotations

from kiro_conduit.runtime.types import RuntimeKind

_RUNTIME_PREFIXES = frozenset({"kiro-cli-acp", "cursor-agent-cli"})


def encode_session_id(kind: RuntimeKind, native_id: str) -> str:
    trimmed = native_id.strip()
    if not trimmed:
        return trimmed
    if ":" in trimmed:
        prefix = trimmed.split(":", 1)[0]
        if prefix in _RUNTIME_PREFIXES:
            return trimmed
    return f"{kind}:{trimmed}"


def decode_session_id(stored: str | None, expected_kind: RuntimeKind) -> str | None:
    if not stored:
        return None
    if ":" in stored:
        prefix, native = stored.split(":", 1)
        if prefix in _RUNTIME_PREFIXES:
            if prefix != expected_kind:
                return None
            return native
    if expected_kind == "kiro-cli-acp":
        return stored
    return None
