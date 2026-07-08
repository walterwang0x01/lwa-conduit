from kiro_conduit.runtime.model_router import resolve_runtime_for_prompt
from kiro_conduit.runtime.types import RuntimeConfig


def test_simple_tier_prefers_balanced(monkeypatch):
    monkeypatch.setattr(
        "kiro_conduit.runtime.model_router.discover_runtime_registry",
        lambda runtime: type(
            "Entry",
            (),
            {
                "models": ["claude-sonnet-4.6", "claude-sonnet-5", "claude-opus-4.8"],
                "default_model": "claude-sonnet-5",
            },
        )(),
    )
    runtime = RuntimeConfig(simple_tier="balanced")
    resolved = resolve_runtime_for_prompt(runtime, "总结这段话", role="test")
    assert resolved.model == "claude-sonnet-4.6"


def test_hard_tier_prefers_max(monkeypatch):
    monkeypatch.setattr(
        "kiro_conduit.runtime.model_router.discover_runtime_registry",
        lambda runtime: type(
            "Entry",
            (),
            {
                "models": ["claude-sonnet-5", "claude-opus-4.8"],
                "default_model": "claude-sonnet-5",
            },
        )(),
    )
    runtime = RuntimeConfig(hard_tier="max", hard_threshold=5)
    prompt = "请在 monorepo 里做跨模块重构，先分析架构，再修改多个文件，最后 review"
    resolved = resolve_runtime_for_prompt(runtime, prompt, role="test")
    assert resolved.model == "claude-opus-4.8"
