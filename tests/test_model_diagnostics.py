from types import SimpleNamespace
from support_copilot import model_on_check


def test_synthetic_diagnostics_do_not_count_timeouts_as_free(monkeypatch, tmp_path):
    monkeypatch.delenv('COPILOT_PRICING_FILE', raising=False)
    monkeypatch.setattr(model_on_check, 'configured_provider', lambda: SimpleNamespace(model='fixture'))
    calls = iter(range(6))
    def fake_triage(*args, **kwargs):
        ok = next(calls) == 0
        return SimpleNamespace(needs_human=True,
            draft_reply=SimpleNamespace(provenance='validated_model_plan' if ok else 'safe_template',
                                        established_facts=[], cited=[], text='fixture'),
            reasons=['draft_model_plan_validated'] if ok else ['draft_provider_timeout', 'draft_safe_template'],
            generation=SimpleNamespace(usage=None, api_cost_usd=None))
    monkeypatch.setattr(model_on_check, 'triage', fake_triage)
    result = model_on_check.run(tmp_path)
    assert result['validated_plans'] == 1
    assert result['fallback_reasons'] == {'draft_provider_timeout': 5}
    assert result['timeout_count'] == 5
    assert result['cost_mean_usd'] is None
    assert result['p95_successful_end_to_end_ms'] is not None
