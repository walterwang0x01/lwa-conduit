from kiro_conduit.runtime.quota import (
    fallback_kinds_for_bucket,
    is_quota_blocked,
    pick_first_available_kind,
    probe_runtime_kind,
)


def test_cursor_depleted_falls_back_to_gemini(monkeypatch) -> None:
    from kiro_conduit.runtime import quota as quota_mod

    quota_mod._CACHE.clear()
    monkeypatch.setenv(
        "KIRO_CONDUIT_QUOTA_OVERRIDES",
        '{"cursor-agent-cli": "depleted"}',
    )
    picked = pick_first_available_kind(fallback_kinds_for_bucket("implementor"))
    assert picked == "gemini-cli"
    assert not is_quota_blocked(probe_runtime_kind(picked))
