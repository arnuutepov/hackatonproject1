import json
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from support_copilot import gemini_provider
from support_copilot.ai_runtime import provider_status, synthetic_check
from support_copilot.api import app
from support_copilot.draft_provider import DraftFailure, configured_provider


def mock_google(monkeypatch, *, tokens=1000, output='{}', usage=None, finish='STOP', error=None):
    calls = []
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return json.dumps(self.data).encode('utf-8')
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert 0 < timeout <= 4
            if error:
                raise HTTPError(request.full_url, error, 'synthetic error', {}, None)
            if request.full_url.endswith(':countTokens'):
                return Response({'totalTokens': tokens})
            return Response({'candidates': [{'finishReason': finish, 'content': {'parts': [{'text': output}]}}],
                             'usageMetadata': usage if usage is not None else
                             {'promptTokenCount': 1000, 'candidatesTokenCount': 70, 'thoughtsTokenCount': 3}})
    monkeypatch.setattr(gemini_provider, 'build_opener', lambda *args: Opener())
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-fixture')
    monkeypatch.delenv('GEMINI_API_KEY_FILE', raising=False)
    monkeypatch.delenv('COPILOT_PRICING_FILE', raising=False)
    monkeypatch.delenv('COPILOT_GEMINI_MODEL', raising=False)
    monkeypatch.delenv('COPILOT_MAX_REQUEST_USD', raising=False)
    monkeypatch.setenv('COPILOT_DRAFT_TIMEOUT', '4')
    return calls


def test_counts_full_request_before_generation_and_accounts_for_thinking(monkeypatch):
    calls = mock_google(monkeypatch)
    provider = configured_provider()
    assert provider.generate({'message': 'example'}) == '{}'
    assert [r.full_url.rsplit(':', 1)[1] for r in calls] == ['countTokens', 'generateContent']
    count = json.loads(calls[0].data)['generateContentRequest']
    generated = json.loads(calls[1].data)
    assert count['model'] == 'models/gemini-2.5-flash-lite'
    assert {k: v for k, v in count.items() if k != 'model'} == generated
    assert generated['generationConfig']['maxOutputTokens'] == 900
    assert generated['generationConfig']['thinkingConfig']['thinkingBudget'] == 0
    assert 'tools' not in generated
    assert all(r.get_header('X-goog-api-key') == 'synthetic-fixture' and '?' not in r.full_url for r in calls)
    assert provider.last_usage == {'input_tokens': 1000, 'output_tokens': 73, 'cached_tokens': 0}
    assert provider.preflight_cost_usd < .002 and provider.generation_started


def test_expensive_request_never_reaches_generation(monkeypatch):
    calls = mock_google(monkeypatch, tokens=20000)
    provider = configured_provider()
    with pytest.raises(DraftFailure, match='budget_exceeded'):
        provider.generate({})
    assert len(calls) == 1 and not provider.generation_started and provider.last_usage is None


@pytest.mark.parametrize('tokens', [None, -1, True, '1000', 0])
def test_missing_or_invalid_token_count_fails_closed(monkeypatch, tokens):
    calls = mock_google(monkeypatch, tokens=tokens)
    with pytest.raises(DraftFailure, match='token_count_unavailable'):
        configured_provider().generate({})
    assert len(calls) == 1


def test_unknown_model_price_is_not_assumed_cheap(monkeypatch):
    calls = mock_google(monkeypatch)
    monkeypatch.setenv('COPILOT_GEMINI_MODEL', 'gemini-unpriced')
    with pytest.raises(DraftFailure, match='budget_unverified'):
        configured_provider().generate({})
    assert calls == []


@pytest.mark.parametrize('budget', ['nan', 'inf', '-1', '0', '0.003', 'garbage'])
def test_budget_cannot_be_disabled_or_raised_above_requirement(monkeypatch, budget):
    calls = mock_google(monkeypatch)
    monkeypatch.setenv('COPILOT_MAX_REQUEST_USD', budget)
    with pytest.raises(DraftFailure): configured_provider()
    assert not calls


@pytest.mark.parametrize('status,reason', [(401, 'authentication'), (403, 'authentication'),
    (404, 'model_unavailable'), (429, 'rate_limited'), (500, 'unavailable')])
def test_error_codes_are_specific_and_never_expose_key(monkeypatch, status, reason):
    mock_google(monkeypatch, error=status)
    with pytest.raises(DraftFailure, match=reason) as exc:
        configured_provider().generate({})
    assert 'synthetic-fixture' not in str(exc.value)


def test_oversized_request_and_redirect_are_blocked(monkeypatch):
    calls = mock_google(monkeypatch)
    with pytest.raises(DraftFailure, match='oversized'):
        configured_provider().generate({'text': 'x' * 25000})
    assert calls == []
    assert gemini_provider.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com') is None


def test_incomplete_response_does_not_erase_usage(monkeypatch):
    mock_google(monkeypatch, finish='MAX_TOKENS')
    provider = configured_provider()
    with pytest.raises(DraftFailure, match='incomplete'): provider.generate({})
    assert provider.last_usage['output_tokens'] == 73


def test_key_file_and_status_never_return_credentials(monkeypatch, tmp_path):
    calls = mock_google(monkeypatch)
    file = tmp_path/'credential'
    file.write_text('fixture-file-key', encoding='utf-8')
    monkeypatch.setenv('GEMINI_API_KEY_FILE', str(file))
    assert configured_provider().api_key == 'fixture-file-key'
    status = provider_status()
    assert status['state'] == 'configured_unverified' and not status['live_verified']
    assert status['assistant_configured'] and not calls
    assert 'fixture-file-key' not in json.dumps(status)
    monkeypatch.setenv('GEMINI_API_KEY_FILE', str(tmp_path/'missing'))
    assert provider_status()['state'] == 'configuration_error'


@pytest.mark.private_data
def test_synthetic_probe_requires_a_valid_plan_and_captures_cost(monkeypatch):
    plan = dict(lang='ru', action='human_review', needs_human=True, claim=None, questions=[], citations=[])
    mock_google(monkeypatch, output=json.dumps(plan))
    result = synthetic_check()
    assert result['ok'] and result['synthetic_only'] and 0 < result['cost_usd'] < .002


@pytest.mark.private_data
def test_budget_rejection_keeps_safe_fallback_and_human_review(monkeypatch):
    from support_copilot.service import triage
    from support_copilot.schemas import TriageRequest
    calls = mock_google(monkeypatch, tokens=20000)
    request = TriageRequest(kind='report', messages=[{'text': 'Оплатил донат, баланс не изменился.'}])
    response = triage(request, use_verified_templates=False)
    assert response.needs_human and response.draft_reply.provenance == 'safe_template'
    assert 'draft_budget_exceeded' in response.reasons and len(calls) == 1


@pytest.mark.private_data
def test_gemini_operator_chat_is_read_only_and_uses_same_provider(monkeypatch):
    calls = mock_google(monkeypatch, output=json.dumps({'answer': 'Уточните дату оплаты.', 'citations': []}))
    client = TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1')
    config = client.get('/operator/config').json()
    assert config['ai']['assistant_configured'] and config['assistant_model'] == 'gemini-2.5-flash-lite'
    ticket = client.post('/operator/tickets', json={'title': 'Synthetic', 'request': {'kind': 'report',
        'messages': [{'text': 'Оплатил донат, баланс не изменился.'}]}}).json()
    response = client.post(f"/operator/tickets/{ticket['id']}/assistant", json={
        'expected_revision': ticket['revision'], 'question': 'Что уточнить?'})
    assert response.status_code == 200
    assert response.json()['provider'] == 'gemini' and response.json()['advisory_only']
    assert response.json()['cost_usd'] > 0
    assert client.get(f"/operator/tickets/{ticket['id']}").json() == ticket
    assert len(calls) == 2


def test_offline_mode_does_not_use_residual_google_or_capybara_keys(monkeypatch):
    calls = mock_google(monkeypatch)
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER', 'template')
    monkeypatch.setenv('CAPYBARA_API_KEY', 'fixture')
    assert not provider_status()['assistant_configured']
    assert synthetic_check()['error'] == 'draft_not_configured'
    assert not calls


def test_count_and_generation_share_the_wall_clock_budget(monkeypatch):
    from types import SimpleNamespace
    calls = mock_google(monkeypatch)
    moments = iter([100.0, 100.0, 105.0])
    monkeypatch.setattr(gemini_provider, 'time', SimpleNamespace(monotonic=lambda: next(moments)))
    provider = configured_provider()
    with pytest.raises(DraftFailure, match='timeout'):
        provider.generate({})
    assert len(calls) == 1 and not provider.generation_started


def test_missing_usage_is_unknown_and_does_not_become_free(monkeypatch):
    mock_google(monkeypatch, usage={})
    provider = configured_provider()
    provider.generate({})
    assert provider.last_usage is None
