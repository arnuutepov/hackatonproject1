"""Opt-in TF-IDF category backend: training mechanics, leakage guards, runtime guards and fallback.

Every text here is invented for code-level checks. The generated rows carry
label_provenance='synthetic' and approved_for_training=false: they are not human gold, and no
accuracy computed on them says anything about real players. Hand-built artifacts below are test
fixtures with fixed weights, not trained models.
"""
import json

import pytest

from support_copilot import category_model, category_training, service
from support_copilot.knowledge import Knowledge
from support_copilot.schemas import TriageRequest

pytest.importorskip('sklearn')

PLACES = ('у банка', 'возле мэрии', 'на парковке', 'у порта', 'в больнице', 'на трассе', 'у казино', 'на заправке')
BASES = {
    'report.help': ('игра вылетает', 'застрял в текстурах', 'персонаж не двигается', 'черный экран'),
    'report.question': ('как купить дом', 'где получить права', 'подскажите цену лицензии', 'сколько стоит машина'),
    'report.offtopic': ('привет всем', 'спасибо за игру', 'хорошего дня', 'просто зашел'),
    'complaint.insult': ('оскорбил меня', 'материт в чате', 'обозвал меня', 'унижает игроков'),
    'complaint.dm': ('убил меня без причины', 'застрелил', 'сбил машиной', 'стреляет в людей'),
    'complaint.cheat': ('летает по карте', 'использует читы', 'стреляет сквозь стены', 'бессмертный'),
}


@pytest.fixture
def knowledge(monkeypatch, tmp_path):
    folder = tmp_path / 'knowledge'
    folder.mkdir()
    (folder / 'rules.json').write_text(json.dumps({'rules': [{'id': '30', 'category': 'Общие', 'points': [
        {'ref': '1.1', 'text': 'Оскорбления игроков запрещены'}]}]}), encoding='utf-8')
    codes = [('report', code) for code in ('payment', 'account', 'complaint', 'help', 'question', 'offtopic')]
    codes += [('complaint', code) for code in ('admin', 'cheat', 'scam', 'rmt_ads', 'insult', 'dm', 'invalid', 'other')]
    (folder / 'taxonomy.json').write_text(json.dumps({
        'version': 'synthetic', 'needs_human_always': ['report.payment', 'report.account'],
        'priorities': [{'code': f'p{level}', 'title': f'P{level}'} for level in range(4)],
        'categories': [{'code': f'{kind}.{code}', 'kind': kind, 'title': code, 'rules': []} for kind, code in codes]}),
        encoding='utf-8')
    value = Knowledge(folder)
    monkeypatch.setattr(service, 'get_knowledge', lambda: value)
    monkeypatch.setattr('support_copilot.drafting.get_knowledge', lambda: value, raising=False)
    monkeypatch.delenv(category_model.ENV_BACKEND, raising=False)
    monkeypatch.delenv(category_model.ENV_MODEL, raising=False)
    monkeypatch.delenv(category_model.ENV_ALLOW_SMOKE, raising=False)
    category_model._CACHE.clear()
    value.taxonomy_path = folder / 'taxonomy.json'
    return value


@pytest.fixture
def no_frozen(tmp_path):
    return tmp_path / 'no-manifest.json'


def synthetic_rows():
    rows = []
    for category, bases in BASES.items():
        kind = category.split('.')[0]
        for base_index, base in enumerate(bases):
            for place_index, place in enumerate(PLACES):
                rows.append({
                    'example_id': f'synthetic-{category}-{base_index}-{place_index}',
                    # Two consecutive places form one dialog group: they must share a split.
                    'group_id': f'synthetic-group-{category}-{base_index}-{place_index // 2}',
                    'split': None,
                    'request': {'kind': kind, 'messages': [{'role': 'player', 'text': f'{base} {place}', 'attachments': 0}]},
                    'category': category, 'label_provenance': 'synthetic', 'approved_for_training': False})
    return rows


def write_rows(path, rows):
    path.write_text(''.join(json.dumps({k: v for k, v in row.items() if k != '_line'}, ensure_ascii=False) + '\n'
                            for row in rows), encoding='utf-8')
    return path


@pytest.fixture
def split_rows():
    rows, _ = category_training.assign_splits(synthetic_rows(), seed=7, test_fraction=.2, validation_fraction=.2)
    return rows


def train(knowledge, tmp_path, rows, name='model', no_frozen=None, **kwargs):
    data = write_rows(tmp_path / f'{name}.jsonl', rows)
    output = tmp_path / name
    report = category_training.train(data, knowledge.taxonomy_path, output, smoke=True, seed=11,
                                     frozen_manifest=no_frozen or tmp_path / 'absent.json', **kwargs)
    return output / 'model.json', report


# --- Data contract --------------------------------------------------------------------------------

def mutate(rows, index, **changes):
    rows = [dict(row) for row in rows]
    rows[index] = {**rows[index], **changes}
    return rows


@pytest.mark.parametrize('change,code', [
    (dict(category='complaint.dm'), 'category_kind_mismatch'),          # row 0 is a report
    (dict(category='report.unknown'), 'category_not_in_taxonomy'),
    (dict(example_id='synthetic-report.help-0-1'), 'duplicate_example_id'),
    (dict(request={'kind': 'report', 'messages': [{'role': 'reported_player', 'text': 'только чужая цитата', 'attachments': 0}]}),
     'no_player_text'),
])
def test_validate_rejects_label_and_kind_inconsistency(knowledge, split_rows, change, code):
    taxonomy = category_training.load_taxonomy(knowledge.taxonomy_path)
    rows = mutate(split_rows, 0, **change)
    with pytest.raises(category_training.DataError) as error:
        category_training.validate(rows, taxonomy)
    assert code in {problem['code'] for problem in error.value.problems}


def test_validate_rejects_group_and_text_leakage_across_splits(knowledge, split_rows):
    taxonomy = category_training.load_taxonomy(knowledge.taxonomy_path)
    first, other = split_rows[0], next(row for row in split_rows if row['split'] != split_rows[0]['split'])
    crossing = mutate(split_rows, split_rows.index(other), group_id=first['group_id'])
    with pytest.raises(category_training.DataError) as error:
        category_training.validate(crossing, taxonomy)
    assert 'group_in_several_splits' in {p['code'] for p in error.value.problems}
    same_text = mutate(split_rows, split_rows.index(other), request=first['request'], category=first['category'])
    with pytest.raises(category_training.DataError) as error:
        category_training.validate(same_text, taxonomy)
    assert 'same_text_in_several_splits' in {p['code'] for p in error.value.problems}


def test_validate_rejects_conflicting_labels_for_the_same_text(knowledge, split_rows):
    taxonomy = category_training.load_taxonomy(knowledge.taxonomy_path)
    twin = {**split_rows[0], 'example_id': 'synthetic-twin', 'category': 'report.question'}
    with pytest.raises(category_training.DataError) as error:
        category_training.validate(split_rows + [twin], taxonomy)
    assert 'conflicting_labels_for_same_text' in {p['code'] for p in error.value.problems}


@pytest.mark.parametrize('extra', [
    {'admin_reply': 'решение администратора'}, {'punishment': 'мут'}, {'verdict': 'approved'},
])
def test_admin_outcomes_cannot_enter_training_rows(tmp_path, extra):
    row = {**synthetic_rows()[0], 'split': 'train', **extra}
    with pytest.raises(category_training.DataError) as error:
        category_training.read_rows(write_rows(tmp_path / 'rows.jsonl', [row]))
    assert error.value.problems[0]['code'] == 'invalid_row'


def test_admin_role_is_rejected_by_the_inference_contract(tmp_path):
    row = synthetic_rows()[0]
    row = {**row, 'split': 'train', 'request': {'kind': 'report', 'messages': [{'role': 'admin', 'text': 'ответ', 'attachments': 0}]}}
    with pytest.raises(category_training.DataError):
        category_training.read_rows(write_rows(tmp_path / 'rows.jsonl', [row]))


def test_frozen_evaluation_texts_cannot_be_trained_on(knowledge, tmp_path, split_rows):
    root = tmp_path / 'project'
    (root / 'evaluation').mkdir(parents=True)
    frozen = root / 'outputs' / 'frozen.jsonl'
    frozen.parent.mkdir()
    # A frozen case whose text equals a training row up to case, digits and nickname masking.
    frozen.write_text(json.dumps({'ticket_id': 'x', 'request': {'kind': 'report', 'messages': [
        {'role': 'player', 'text': split_rows[3]['request']['messages'][0]['text'].upper(), 'attachments': 0}]}},
        ensure_ascii=False) + '\n', encoding='utf-8')
    manifest = root / 'evaluation' / 'frozen-inputs.json'
    manifest.write_text(json.dumps([{'path': 'outputs/frozen.jsonl', 'sha256': 'unused'}]))
    keys, status = category_training.frozen_text_keys(manifest)
    assert status == 'checked'
    with pytest.raises(category_training.DataError) as error:
        category_training.validate(split_rows, category_training.load_taxonomy(knowledge.taxonomy_path), frozen_keys=keys)
    assert error.value.problems[0] == {'code': 'overlaps_frozen_evaluation_inputs',
                                       'example_ids': [split_rows[3]['example_id']]}


# --- Group split ----------------------------------------------------------------------------------

def test_group_split_keeps_groups_and_identical_texts_together(knowledge):
    rows = synthetic_rows()
    # The same normalized text under another group and another nickname must follow its twin.
    twin = {**rows[5], 'example_id': 'synthetic-twin', 'group_id': 'synthetic-other-group',
            'request': {'kind': 'report', 'messages': [
                {'role': 'player', 'text': rows[5]['request']['messages'][0]['text'].upper(), 'attachments': 0}]}}
    result, info = category_training.assign_splits(rows + [twin], seed=3, test_fraction=.2, validation_fraction=.2)
    splits_by_group = {}
    for row in result:
        splits_by_group.setdefault(row['group_id'], set()).add(row['split'])
    assert all(len(splits) == 1 for splits in splits_by_group.values())
    assert result[5]['split'] == result[-1]['split']
    assert {row['split'] for row in result} == {'train', 'validation', 'test'}
    assert info['methods']['test'].startswith('stratified_group_kfold')
    category_training.validate(result, category_training.load_taxonomy(knowledge.taxonomy_path))


def test_group_split_is_seeded_and_refuses_to_reshuffle():
    first, _ = category_training.assign_splits(synthetic_rows(), seed=5, test_fraction=.2, validation_fraction=.2)
    again, _ = category_training.assign_splits(synthetic_rows(), seed=5, test_fraction=.2, validation_fraction=.2)
    other, _ = category_training.assign_splits(synthetic_rows(), seed=6, test_fraction=.2, validation_fraction=.2)
    assert [r['split'] for r in first] == [r['split'] for r in again]
    assert [r['split'] for r in first] != [r['split'] for r in other]
    with pytest.raises(ValueError, match='reshuffle'):
        category_training.assign_splits(first, seed=5, test_fraction=.2, validation_fraction=.2)


# --- Training -------------------------------------------------------------------------------------

def test_only_approved_human_labels_are_trainable_by_default():
    base = {'label_provenance': 'human', 'approved_for_training': True}
    assert category_training.trainable(base, smoke=False)
    assert not category_training.trainable({**base, 'approved_for_training': False}, smoke=False)
    for provenance in ('assistant_silver', 'synthetic', 'feedback_unconfirmed'):
        assert not category_training.trainable({**base, 'label_provenance': provenance}, smoke=False)
    assert category_training.trainable({'label_provenance': 'synthetic', 'approved_for_training': False}, smoke=True)
    assert not category_training.trainable({'label_provenance': 'assistant_silver', 'approved_for_training': True}, smoke=True)


def test_synthetic_rows_never_train_a_candidate_model(knowledge, tmp_path, split_rows):
    data = write_rows(tmp_path / 'rows.jsonl', split_rows)
    with pytest.raises(category_training.DataError) as error:
        category_training.train(data, knowledge.taxonomy_path, tmp_path / 'out', frozen_manifest=tmp_path / 'absent')
    assert error.value.problems[0]['code'] == 'nothing_trainable'
    assert not (tmp_path / 'out' / 'model.json').exists()


def test_training_metadata_records_reproducibility_inputs(knowledge, tmp_path, split_rows):
    path, report = train(knowledge, tmp_path, split_rows)
    body = json.loads(path.read_text(encoding='utf-8'))['body']
    assert body['purpose'] == 'smoke_test_only'
    training = body['training']
    assert training['seed'] == 11
    assert set(training['dependencies']) >= {'python', 'scikit-learn', 'numpy', 'scipy'}
    assert training['data']['used']['train'] and training['data']['used']['validation']
    assert training['data']['summary']['by_split']['test']  # counted, never used
    assert training['data']['requires_approved_for_training'] is False
    assert len(training['data']['file_sha256']) == 64 and training['implementation_sha256']
    assert body['taxonomy_sha256'] == knowledge.hashes['taxonomy']
    for kind in ('report', 'complaint'):
        spec = body['kinds'][kind]
        assert spec['score']['selected_on'] == 'validation'
        assert set(spec['classifier']['classes']) == {c for c in BASES if c.startswith(kind)}
        assert 'report.payment' in spec.get('taxonomy_classes_absent_from_train', []) or kind == 'complaint'
        candidates = report['kinds'][kind]['candidates']
        features = {item['candidate']['features'] for item in candidates}
        classifiers = {item['candidate']['classifier'] for item in candidates}
        assert features == {'word_1_2', 'char_wb_3_5', 'word_1_2+char_wb_3_5'}
        assert classifiers == {'linear_svc', 'logistic_regression'}
        selection = report['kinds'][kind]['selection']
        chosen = max(selection['threshold_search'],
                     key=lambda row: (row['accuracy'], row['macro_f1_all'], -row['changed_vs_rules']))
        assert spec['score']['threshold'] == chosen['threshold']


def test_training_is_repeatable_byte_for_byte(knowledge, tmp_path, split_rows):
    first, _ = train(knowledge, tmp_path, split_rows, name='first')
    second, _ = train(knowledge, tmp_path, split_rows, name='second')
    assert first.read_bytes() == second.read_bytes()


def test_test_split_never_influences_the_fitted_model(knowledge, tmp_path, split_rows):
    baseline, _ = train(knowledge, tmp_path, split_rows, name='baseline')
    changed = []
    for row in split_rows:
        if row['split'] == 'test':
            kind = row['request']['kind']
            row = {**row, 'category': f'{kind}.help' if kind == 'report' else 'complaint.dm',
                   'request': {'kind': kind, 'messages': [
                       {'role': 'player', 'text': 'уникальноеслово ' + row['example_id'], 'attachments': 0}]}}
        changed.append(row)
    modified, _ = train(knowledge, tmp_path, changed, name='modified')
    kinds = [json.loads(path.read_text(encoding='utf-8'))['body']['kinds'] for path in (baseline, modified)]
    assert kinds[0] == kinds[1]


def test_vectorizer_is_fitted_on_train_only_and_ignores_non_player_fields(knowledge, tmp_path, split_rows):
    rows = []
    for row in split_rows:
        messages = list(row['request']['messages'])
        if row['split'] != 'train':
            messages[0] = {**messages[0], 'text': messages[0]['text'] + ' heldoutmarker'}
        # A reported player's quote and identifiers carry markers that must never become features.
        messages.append({'role': 'reported_player', 'text': 'reportedmarker цитата', 'attachments': 0})
        rows.append({**row, 'example_id': row['example_id'] + '-idmarker', 'group_id': row['group_id'] + '-groupmarker',
                     'request': {**row['request'], 'messages': messages}})
    path, _ = train(knowledge, tmp_path, rows)
    body = json.loads(path.read_text(encoding='utf-8'))['body']
    vocabulary = ' '.join(feature for spec in body['kinds'].values()
                          for block in spec['vectorizer_blocks'] for feature in block['vocabulary'])
    for marker in ('heldoutmarker', 'reportedmarker', 'idmarker', 'groupmarker', 'synthetic', 'report.', 'complaint.'):
        assert marker not in vocabulary
    assert 'w:банка' in vocabulary or 'c: ба' in vocabulary


@pytest.mark.parametrize('features', list(category_training.FEATURE_SETS))
@pytest.mark.parametrize('classifier', ['linear_svc', 'logistic_regression'])
@pytest.mark.parametrize('binary', [False, True])
def test_exported_json_inference_matches_scikit_learn(features, classifier, binary):
    rows = [row for row in synthetic_rows() if row['request']['kind'] == 'report']
    if binary:
        rows = [row for row in rows if row['category'] in ('report.help', 'report.question')]
    texts = [category_model.player_text(row['request']) for row in rows]
    labels = [row['category'] for row in rows]
    candidate = {'name': 'probe', 'features': features, 'classifier': classifier, 'C': 1.0, 'class_weight': None}
    vectorizers, model, _ = category_training.fit(candidate, texts, labels, seed=1)
    exported = category_model._kind_model('report', category_training.export_kind(candidate, vectorizers, model, 0.0, {}))
    probes = texts[:6] + ['Игра вылетает у Night_Wolf на сервере 12', 'совсем новые слова', 'игра']
    expected = model.decision_function(category_training.transform(vectorizers, probes))
    for text, row in zip(probes, expected):
        values = exported.decision(text)
        reference = [float(row)] if binary else [float(v) for v in row]
        assert values == pytest.approx(reference, abs=1e-9)
        assert exported.predict(text)[0] == model.predict(category_training.transform(vectorizers, [text]))[0]


def test_masking_removes_identities_and_numbers():
    view = category_model.preprocess('Night_Wolf убил на сервере 12, пруф https://example.org/x и t.me/abc')
    assert 'night_wolf' not in view and '12' not in view and 'example' not in view
    assert 'nicktoken' in view and 'urltoken' in view and '0' in view


# --- Hand-built artifact for runtime guards ------------------------------------------------------

def handmade(path, knowledge, *, purpose='candidate', threshold=.5, taxonomy=None):
    def kind(classes, weights):
        vocabulary = sorted(weights)
        coef = [[weights[word].get(code, 0.0) for word in vocabulary] for code in classes]
        return {'vectorizer_blocks': [{'analyzer': {'type': 'word', 'ngram': [1, 1]}, 'vocabulary': vocabulary,
                                       'sublinear_tf': True, 'norm': 'l2', 'idf': [1.0] * len(vocabulary)}],
                'classifier': {'type': 'linear_svc', 'classes': classes, 'coef': coef, 'intercept': [0.0] * len(classes)},
                'score': {'name': category_model.SCORE_NAMES['linear_svc'], 'threshold': threshold, 'selected_on': 'validation'},
                'selected_candidate': 'handmade-test-fixture'}
    body = {
        'purpose': purpose, 'trainer': 'handmade-test-fixture',
        'preprocessing': {'version': category_model.PREPROCESSING_VERSION},
        'taxonomy_sha256': taxonomy or knowledge.hashes['taxonomy'],
        'kinds': {
            'report': kind(['report.complaint', 'report.help', 'report.question'], {
                'w:персонаж': {'report.help': 2.0}, 'w:донат': {'report.question': 2.0},
                'w:запись': {'report.complaint': 2.0}, 'w:zastrial': {'report.question': 2.0},
                'w:пожаловаться': {'report.help': 2.0}}),
            'complaint': kind(['complaint.admin', 'complaint.cheat', 'complaint.other'], {
                'w:софт': {'complaint.cheat': 2.0}, 'w:наказание': {'complaint.other': 2.0}}),
        },
        'training': {'leakage_guards': {'group_digests': [], 'text_digests': []}},
    }
    category_model.write_artifact(body, path)
    return path


def run(text, kind='report'):
    return service.triage(TriageRequest(kind=kind, messages=[{'role': 'player', 'text': text}]),
                          use_configured_provider=False)


@pytest.fixture
def tfidf(knowledge, tmp_path, monkeypatch):
    path = handmade(tmp_path / 'handmade' / 'model.json', knowledge)
    monkeypatch.setenv(category_model.ENV_BACKEND, 'tfidf')
    monkeypatch.setenv(category_model.ENV_MODEL, str(path))
    return path


TEXTS = [('Персонаж не шевелится после гаража', 'report'), ('Оплатил донат, баланс не изменился', 'report'),
         ('Хочу пожаловаться на игрока Night_Wolf, нарушает правила', 'report'), ('Хочу пересмотреть запись', 'report'),
         ('Zastrial u bolnicy', 'report'), ('Прошу пересмотреть наказание', 'complaint'),
         ('Хочу установить софт для записи видео', 'complaint'), ('Взломали аккаунт', 'report'),
         ('Читер стреляет через стены', 'complaint'), ('Привет всем', 'report')]


def test_default_backend_is_unchanged_and_never_reads_the_model(knowledge, tmp_path, monkeypatch):
    baseline = [run(text, kind).model_dump() for text, kind in TEXTS]
    assert not any(reason.startswith('category_') for item in baseline for reason in item['reasons'])
    assert {item['confidence_method'] for item in baseline} == {'lexical_heuristic_uncalibrated'}
    monkeypatch.setenv(category_model.ENV_MODEL, str(tmp_path / 'missing.json'))
    for value in ('rules', ''):
        monkeypatch.setenv(category_model.ENV_BACKEND, value)
        assert [run(text, kind).model_dump() for text, kind in TEXTS] == baseline


def test_model_category_is_used_above_the_validation_threshold(tfidf):
    result = run('Персонаж не шевелится после гаража')
    assert result.category == 'report.help'
    assert 'category_source:model' in result.reasons
    assert result.confidence_method == 'tfidf_linear_validation_threshold_uncalibrated'
    # The rule core had no category evidence: the case stays incomplete and with a human.
    assert result.needs_human and result.confidence <= .25 and result.confidence_level == 'low'
    assert result.draft_reply.action == 'human_review'


def test_below_threshold_keeps_the_rule_category(tfidf):
    result = run('Привет всем')
    assert result.category == 'report.offtopic'
    assert 'category_model_abstained:below_validation_threshold' in result.reasons


def test_escalation_of_the_rule_category_survives_a_model_change(tfidf):
    result = run('Хочу пожаловаться на игрока Night_Wolf, нарушает правила')
    assert result.category == 'report.help'          # the fixture's model disagrees with the rules
    assert result.needs_human                        # report.complaint from the rules still demands a human
    assert 'human_review_required_no_personal_action' in result.reasons
    assert result.draft_reply.action == 'human_review'


def test_mandatory_payment_and_account_are_never_overridden(tfidf):
    for text, expected in [('Оплатил донат, баланс не изменился', 'report.payment'), ('Взломали аккаунт', 'report.account')]:
        result = run(text)
        assert result.category == expected and result.priority == 'p0' and result.needs_human
        assert 'category_guard:mandatory_rule_category' in result.reasons


def test_appeal_decision_is_never_overridden(tfidf):
    result = run('Прошу пересмотреть наказание', 'complaint')
    assert result.category == 'complaint.admin'
    assert 'category_guard:appeal_rule' in result.reasons and result.needs_human


def test_rewatching_a_recording_cannot_become_an_appeal_through_the_model(tfidf):
    result = run('Хочу пересмотреть запись')
    assert result.category == 'report.offtopic'
    assert 'category_guard:rewatch_not_appeal' in result.reasons
    assert 'category_guard:appeal_rule' not in result.reasons and 'admin' not in result.detected_topics


def test_ordinary_software_cannot_become_cheating_through_the_model(tfidf):
    result = run('Хочу установить софт для записи видео', 'complaint')
    assert result.category != 'complaint.cheat'
    assert 'category_guard:ordinary_software_not_cheat' in result.reasons
    assert 'cheat' not in result.detected_topics and result.priority != 'p1'


def test_independent_cheat_evidence_is_not_vetoed(tfidf):
    result = run('Скачал софт, теперь использует читы и стреляет через стены', 'complaint')
    assert 'category_guard:ordinary_software_not_cheat' not in result.reasons
    assert result.priority == 'p1'


def test_transliterated_input_keeps_human_review_with_a_model_category(tfidf):
    result = run('Zastrial u bolnicy')
    assert result.category == 'report.question'      # fixture weights, not a claim about quality
    assert result.lang == 'ru' and result.needs_human and result.confidence_level == 'low'
    assert 'transliterated_input_requires_review' in result.reasons


def test_shadow_mode_records_the_candidate_and_keeps_rules(knowledge, tfidf, monkeypatch):
    monkeypatch.setenv(category_model.ENV_BACKEND, 'shadow')
    result = run('Хочу пожаловаться на игрока Night_Wolf, нарушает правила')
    assert result.category == 'report.complaint'
    assert 'category_shadow_candidate:report.help' in result.reasons
    assert 'category_shadow_would_be:accepted' in result.reasons
    assert result.confidence_method == 'lexical_heuristic_uncalibrated'


def test_model_never_lowers_any_rule_escalation_priority_or_flag(knowledge, tfidf, monkeypatch):
    monkeypatch.setenv(category_model.ENV_BACKEND, 'rules')
    rules = [run(text, kind) for text, kind in TEXTS]
    monkeypatch.setenv(category_model.ENV_BACKEND, 'tfidf')
    model = [run(text, kind) for text, kind in TEXTS]
    for before, after in zip(rules, model):
        assert after.needs_human >= before.needs_human
        assert after.safety_flags == before.safety_flags and after.detected_topics == before.detected_topics
        assert (after.priority in ('p0', 'p1')) == (before.priority in ('p0', 'p1'))


# --- Fallback ------------------------------------------------------------------------------------

def fallback_reason(result):
    return [reason for reason in result.reasons if reason.startswith('category_model_unavailable:')]


def test_missing_artifact_falls_back_with_a_diagnostic(knowledge, tmp_path, monkeypatch):
    monkeypatch.setenv(category_model.ENV_BACKEND, 'tfidf')
    monkeypatch.setenv(category_model.ENV_MODEL, str(tmp_path / 'missing.json'))
    result = run('Персонаж не шевелится после гаража')
    assert result.category == 'report.offtopic'
    assert fallback_reason(result) == ['category_model_unavailable:artifact_missing']


@pytest.mark.parametrize('corrupt,code', [
    (lambda raw: raw.replace('"report.help"', '"report.hack"', 1), 'artifact_checksum_mismatch'),
    (lambda raw: raw[:len(raw) // 2], 'artifact_unreadable'),
    (lambda raw: raw.replace(category_model.FORMAT, 'pickle-v0'), 'artifact_format_unknown'),
])
def test_corrupted_artifact_falls_back_with_a_diagnostic(knowledge, tfidf, corrupt, code):
    tfidf.write_text(corrupt(tfidf.read_text(encoding='utf-8')), encoding='utf-8')
    result = run('Персонаж не шевелится после гаража')
    assert result.category == 'report.offtopic'
    assert fallback_reason(result) == ['category_model_unavailable:' + code]


def test_taxonomy_mismatch_and_smoke_artifacts_fall_back(knowledge, tmp_path, monkeypatch):
    monkeypatch.setenv(category_model.ENV_BACKEND, 'tfidf')
    other = handmade(tmp_path / 'other.json', knowledge, taxonomy='0' * 64)
    monkeypatch.setenv(category_model.ENV_MODEL, str(other))
    assert fallback_reason(run('Персонаж не двигается')) == ['category_model_unavailable:taxonomy_mismatch']
    smoke = handmade(tmp_path / 'smoke.json', knowledge, purpose='smoke_test_only')
    monkeypatch.setenv(category_model.ENV_MODEL, str(smoke))
    assert fallback_reason(run('Персонаж не двигается')) == ['category_model_unavailable:smoke_model_not_allowed']
    monkeypatch.setenv(category_model.ENV_ALLOW_SMOKE, '1')
    assert run('Персонаж не двигается').category == 'report.help'


def test_unknown_backend_value_uses_rules_and_says_so(knowledge, monkeypatch):
    monkeypatch.setenv(category_model.ENV_BACKEND, 'llm')
    result = run('Персонаж не шевелится после гаража')
    assert result.category == 'report.offtopic'
    assert 'category_backend_unknown_value_rules_used' in result.reasons


def test_check_command_and_report_metadata(knowledge, tfidf, tmp_path, capsys):
    assert category_model.main(['check', '--model', str(tfidf)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ok'
    assert category_model.main(['check', '--model', str(tmp_path / 'missing.json')]) == 2
    metadata = category_model.backend_metadata()
    assert metadata['backend'] == 'tfidf' and metadata['status'] == 'loaded' and len(metadata['model_sha256']) == 64


# --- Same switch for CLI and HTTP ----------------------------------------------------------------

def test_cli_batch_matches_service_with_the_model_backend(tfidf, tmp_path):
    from support_copilot.cli import predict
    source, output = tmp_path / 'in.jsonl', tmp_path / 'out.jsonl'
    payloads = [{'kind': kind, 'messages': [{'role': 'player', 'text': text, 'attachments': 0}]} for text, kind in TEXTS]
    source.write_text(''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in payloads), encoding='utf-8')
    assert predict(source, output) == len(payloads)
    lines = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
    assert lines == [service.triage(TriageRequest.model_validate(p)).model_dump() for p in payloads]
    assert lines[0]['category'] == 'report.help'


def test_http_matches_service_with_the_model_backend(tfidf):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    from support_copilot.api import app
    client = TestClient(app, client=('127.0.0.1', 40000), base_url='http://127.0.0.1')
    payload = {'kind': 'report', 'messages': [{'role': 'player', 'text': 'Персонаж не шевелится после гаража', 'attachments': 0}]}
    response = client.post('/triage', json=payload)
    assert response.status_code == 200
    assert response.json() == service.triage(TriageRequest.model_validate(payload)).model_dump()
    assert response.json()['category'] == 'report.help' and response.json()['needs_human']


# --- End-to-end smoke through the command line ---------------------------------------------------

def test_command_line_smoke_train_and_labelled_evaluation(knowledge, tmp_path, monkeypatch, capsys, split_rows):
    data = write_rows(tmp_path / 'rows.jsonl', split_rows)
    absent = str(tmp_path / 'absent.json')
    common = ['--data', str(data), '--taxonomy', str(knowledge.taxonomy_path), '--frozen-manifest', absent]
    assert category_training.main(['validate', *common]) == 0
    assert category_training.main(['train', *common, '--output', str(tmp_path / 'out'), '--smoke-synthetic']) == 0
    capsys.readouterr()
    model = str(tmp_path / 'out' / 'model.json')
    assert category_training.main(['evaluate', *common, '--model', model]) == 2
    assert 'non_human_labels_in_holdout' in capsys.readouterr().out
    report_path = tmp_path / 'eval.json'
    assert category_training.main(['evaluate', *common, '--model', model, '--allow-non-human',
                                   '--output', str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding='utf-8'))
    assert report['protocol'] == 'category-holdout-v1' and report['labels_are_human_gold'] is False
    assert report['limitations'][0].startswith('Not a quality estimate on players')
    assert set(report['variants']) == {'rules', 'model_only', 'hybrid_runtime'}
    assert report['hybrid_vs_rules']['mandatory_rule_category_overridden'] == 0
    assert report['n'] == sum(row['split'] == 'test' for row in split_rows)


def test_evaluation_refuses_a_holdout_that_overlaps_training(knowledge, tmp_path, split_rows, monkeypatch):
    path, _ = train(knowledge, tmp_path, split_rows)
    leaked = [{**row, 'split': 'test'} if row['split'] == 'train' else row for row in split_rows]
    monkeypatch.setenv(category_model.ENV_ALLOW_SMOKE, '1')
    with pytest.raises(category_training.DataError) as error:
        category_training.evaluate(path, write_rows(tmp_path / 'leaked.jsonl', leaked), knowledge.taxonomy_path,
                                   allow_non_human=True, frozen_manifest=tmp_path / 'absent')
    assert error.value.problems[0]['code'] == 'holdout_overlaps_training'
