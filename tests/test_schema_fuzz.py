"""Optional Schemathesis gate against the real inference contract, with synthetic knowledge in CI."""
import pytest
schemathesis=pytest.importorskip('schemathesis')
from hypothesis import settings
from pathlib import Path
import tempfile
from support_copilot.api import app

schema=schemathesis.openapi.from_dict(app.openapi())


@schema.include(path='/triage',method='POST').parametrize()
@settings(max_examples=20,deadline=None)
def test_triage_contract(case):
    # Each generated example gets a fresh database and environment context.
    with tempfile.TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as monkeypatch:
        tmp_path=Path(directory)
        monkeypatch.setenv('COPILOT_OPERATOR_DB',str(tmp_path/'workspace.sqlite3'))
        monkeypatch.setenv('COPILOT_AUTH_DB',str(tmp_path/'auth.sqlite3'))
        monkeypatch.setenv('COPILOT_AUTH_MODE','required')
        monkeypatch.setenv('COPILOT_TRIAGE_TOKEN','synthetic-schema-token')
        # Keep real validation/service/safety/rendering; only replace organizer dictionaries in CI.
        import os
        if os.environ.get('COPILOT_CI_SYNTHETIC')=='1':
            from support_copilot.knowledge import Knowledge
            import json
            root=tmp_path/'knowledge';root.mkdir()
            categories=[{'code':'report.'+c,'kind':'report','title':c,'rules':[]} for c in ('question','help','account','payment','complaint','offtopic')]
            categories += [{'code':'complaint.'+c,'kind':'complaint','title':c,'rules':[]} for c in ('admin','cheat','scam','rmt_ads','insult','dm','invalid','other')]
            (root/'taxonomy.json').write_text(json.dumps({'version':'synthetic-ci','categories':categories,
                'priorities':[{'code':'p'+str(i),'title':str(i)} for i in range(4)],'needs_human_always':['report.account','report.payment']}))
            (root/'rules.json').write_text('{"rules":[]}')
            knowledge=Knowledge(root)
            monkeypatch.setattr('support_copilot.service.get_knowledge',lambda:knowledge)
            monkeypatch.setattr('support_copilot.drafting.get_knowledge',lambda:knowledge,raising=False)
            monkeypatch.setattr('support_copilot.knowledge.get_knowledge',lambda:knowledge)
        response=case.call(app=app,base_url='http://localhost',headers={'Authorization':'Bearer synthetic-schema-token'})
        case.validate_response(response,checks=[schemathesis.checks.not_a_server_error,schemathesis.checks.response_schema_conformance])
