import json
import pytest
from support_copilot.language_id import language
from support_copilot.quality import bootstrap,kappa
from support_copilot.dedup import simhash,near_duplicate
from support_copilot.privacy import redact,redact_tree,retain


@pytest.mark.parametrize('text,expected',[
 ('Сәлем, аккаунтқа кіре алмаймын!','kk'),('Мені балағаттады','kk'),
 ("Salom, to‘lov ishlamayapti",'uz'),('Нужна помощь с игрой','ru'),
 ('поможіть будь ласка','und'),('памагі застрял','ru'),
 ('H3lp please','en'),('Помогите, мне вьідали предупреждение','ru'),
 ('Қғ','und'),('hola por favor','es'),('olá ajuda minha conta','pt')])
def test_language_script_overlap(text,expected):assert language(text)[0]==expected


def test_bootstrap_and_degenerate_kappa():
    assert bootstrap([1]*10)['low']==1
    assert bootstrap([]) is None
    assert bootstrap([0,1,1])==bootstrap([0,1,1])
    assert kappa(['a','b'],['a','b'])['kappa']==1
    assert kappa(['a'],['a'])['kappa'] is None
    with pytest.raises(ValueError):kappa([],[])


def test_checked_rows_reads_utf8_independently_of_windows_codepage(tmp_path, monkeypatch):
    import hashlib
    import support_copilot.quality as quality
    (tmp_path / 'evaluation').mkdir()
    source = tmp_path / 'sample.jsonl'
    source.write_text(json.dumps({'text': 'Қазақша oʻ'}, ensure_ascii=False) + '\n', encoding='utf-8')
    manifest = [{'path': 'sample.jsonl', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
    (tmp_path / 'evaluation' / 'frozen-inputs.json').write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(quality, 'ROOT', tmp_path)
    assert quality.checked_rows(source) == [{'text': 'Қазақша oʻ'}]


def test_simhash_preserves_distinct_short_requests():
    assert simhash('Оплатил 100 рублей')==simhash('оплатил 100 РУБЛЕЙ!')
    assert not near_duplicate('помогите','помогите')
    a='Игрок застрял внутри стены на сервере семь возле здания больницы после входа в игру'
    assert near_duplicate(a,a+'!')
    assert not near_duplicate(a,'Оплата банковской картой прошла но баланс не пополнился на сервере девять')


def test_redaction_keeps_control_fields_and_removes_contact():
    original={'needs_human_minimum':True,'allowed_questions':['payment_details'],
              'messages':[{'text':'Мой ник Test_Player и почта player@example.org'}]}
    value=redact_tree(original)
    assert value['allowed_questions']==['payment_details'] and value['needs_human_minimum']
    assert 'Test_Player' not in value['messages'][0]['text']
    wire=redact_tree([{'role':'user','content':json.dumps(original)}])
    assert json.loads(wire[0]['content'])==value
    assert 'player@example.org' not in redact('player@example.org')


def test_rollback_never_publishes_identity_outbox():
    from support_copilot.auth_store import AuthStore
    from support_copilot.audit_chain import AuditChain
    auth=AuthStore()
    with pytest.raises(RuntimeError):
        with auth.connect() as db:
            auth._journal(db,'operator.login','test',{'id':'test'})
            raise RuntimeError('transaction aborted')
    assert AuditChain().head() is None
    with auth.connect() as db:assert db.execute('SELECT count(*) FROM identity_chain_outbox').fetchone()[0]==0


def test_retention_dry_run_then_apply_leaves_open_and_chain():
    from support_copilot.operator_store import OperatorStore
    store=OperatorStore()
    request={'kind':'report','messages':[{'role':'player','text':'Тестовое обращение','attachments':0}]}
    old=store.create('Old',request);opened=store.create('Open',request)
    store.chain.genesis()
    with store.connect() as db:
        db.execute("UPDATE tickets SET updated_at='2000-01-01T00:00:00+00:00',status='resolved' WHERE id=?",(old,))
    assert retain(store)['eligible_closed_tickets']==1 and len(store.list())==2
    assert retain(store,apply=True)['deleted']==1
    assert store.get(opened)['id']==opened and store.chain.verify()['valid']


@pytest.mark.private_data
def test_feedback_requires_second_reviewer_and_preserves_inference(tmp_path):
    from support_copilot.operator_store import OperatorStore
    from support_copilot.service import triage
    from support_copilot.schemas import TriageRequest
    from support_copilot.feedback import approve,export
    store=OperatorStore(actor='first')
    request=TriageRequest(kind='report',messages=[{'role':'player','text':'Застрял в текстурах','attachments':0}])
    key=store.create('Test',request.model_dump());result=triage(request,use_configured_provider=False)
    store.analyze(key,1,result.model_dump(),1,{})
    ticket=store.get(key);store.decide(key,ticket['revision'],'Уточните сервер и место.','save','')
    with store.connect() as db:fid=db.execute('SELECT id FROM operator_feedback').fetchone()[0]
    labels={'category':'report.help','priority':'p2','lang':'ru','needs_human':False}
    with pytest.raises(ValueError):approve(store,fid,labels,'first')
    approve(store,fid,labels,'second')
    assert store.get(key)['analysis']==ticket['analysis']
    output=tmp_path/'labels.jsonl';assert export(store,output)==1
    value=json.loads(output.read_text());assert not value['eligible_for_training'] and 'request' not in value

@pytest.mark.private_data
def test_retrieval_does_not_fill_unrelated_short_help_with_rules():
    from support_copilot.knowledge import get_knowledge
    assert get_knowledge().search('Застрял в текстурах')==[]


def test_gold_packet_never_overwrites_human_ratings(tmp_path):
    from support_copilot.quality import prepare_gold
    file=tmp_path/'rater-a.json';file.write_text('{"rater_id":"human-a","ratings":[]}')
    with pytest.raises(ValueError,match='overwrite'):prepare_gold([],tmp_path)
    assert 'human-a' in file.read_text()
