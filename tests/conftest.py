import pytest


@pytest.fixture(autouse=True)
def offline_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv('COPILOT_AUTH_MODE', 'local')
    monkeypatch.setenv('COPILOT_AUTH_DB', str(tmp_path / 'auth.sqlite3'))
    monkeypatch.delenv('COPILOT_TRIAGE_TOKEN', raising=False)
    monkeypatch.delenv('COPILOT_GAME_HMAC_KEYS', raising=False)
    monkeypatch.delenv('COPILOT_PUBLIC_ORIGIN', raising=False)
    # Running tests never inherits a user's opt-in paid provider configuration.
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'template')
    monkeypatch.setenv('COPILOT_VERIFIED_TEMPLATES','off')
