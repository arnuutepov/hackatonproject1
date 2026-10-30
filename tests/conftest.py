import pytest


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch):
    # Running tests never inherits a user's opt-in paid provider configuration.
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'template')
