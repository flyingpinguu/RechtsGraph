"""Keep the offline test suite independent of local paid-provider settings."""
import pytest


@pytest.fixture(autouse=True)
def isolate_paid_provider_configuration(monkeypatch):
    monkeypatch.setenv("RECHTSGRAPH_LOAD_ENV", "0")
    monkeypatch.setenv("RECHTSGRAPH_SEARCH_MODE", "lexical")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
