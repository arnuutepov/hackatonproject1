from types import SimpleNamespace

import pytest

from support_copilot import project_metrics, quality


def test_breakdown_baselines_and_missing_citation_denominator(monkeypatch):
    def prediction(category, priority):
        return SimpleNamespace(category=category, priority=priority, lang='ru', needs_human=True,
            confidence_level='low', draft_reply=SimpleNamespace(cited=[], provenance='safe_template'), retrieved_rules=[])
    predictions = iter([prediction('report.help','p1'), prediction('report.help','p1'), prediction('report.question','p2')])
    knowledge = SimpleNamespace(categories=['report.help','report.question'], priorities=['p0','p1','p2','p3'], rules={})
    monkeypatch.setattr(quality, 'get_knowledge', lambda: knowledge)
    monkeypatch.setattr(quality, 'triage', lambda *args, **kwargs: next(predictions))
    rows = [dict(request={'kind':'report','messages':[{'text':'Synthetic'}]}, category=cat, priority=priority,
                 lang='ru', needs_human=human) for cat, priority, human in [
                 ('report.help','p1',True), ('report.question','p2',False), ('report.question','p2',True)]]
    result = quality.metrics(rows)
    assert result['category']['estimate'] == 2/3
    assert result['priority_macro_f1'] == pytest.approx(1/3)
    assert result['baselines']['majority_category_per_kind_accuracy'] == 2/3
    assert result['baselines']['always_escalate']['precision'] == 2/3
    assert result['citations']['returned'] == 0 and result['citations']['error_rate'] is None
    assert result['classification']['category']['per_class']['report.question']['support'] == 2


def test_scorecard_does_not_read_control_or_call_live_provider(monkeypatch):
    from support_copilot import knowledge, evaluate_drafts, evaluate_regressions
    monkeypatch.setenv('COPILOT_DRAFT_PROVIDER','gemini')
    monkeypatch.setenv('COPILOT_OPERATOR_DB','original.sqlite3')
    monkeypatch.setattr(knowledge,'get_knowledge',lambda: SimpleNamespace(rules={'example':None}, hashes={}))
    monkeypatch.setattr(project_metrics,'retrieval_metrics',lambda: {})
    monkeypatch.setattr(project_metrics,'checked_rows',lambda path: pytest.fail('Private inputs must not be read'))
    def offline():
        assert project_metrics.os.environ['COPILOT_DRAFT_PROVIDER'] == 'template'
        return dict(passed=1,runs=1)
    monkeypatch.setattr(evaluate_drafts,'evaluate',offline)
    monkeypatch.setattr(evaluate_regressions,'evaluate',offline)
    report = project_metrics.run(dev_file=None)
    assert not report['control_evaluated'] and report['model_api_calls'] == 0
    assert report['overall_jury_score'] is None and report['dev'] is None
    assert project_metrics.os.environ['COPILOT_DRAFT_PROVIDER'] == 'gemini'
    assert project_metrics.os.environ['COPILOT_OPERATOR_DB'] == 'original.sqlite3'
