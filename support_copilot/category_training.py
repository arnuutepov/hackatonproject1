"""Train, compare and evaluate the opt-in TF-IDF + linear category backend on local labeled data.

Offline tool for the team's machine; requires the optional `train` extra (scikit-learn, numpy, scipy).
Runtime inference lives in category_model.py and does not import this module.

Data contract: one JSON object per line, see docs/category-model.md and DATA_CONTRACT.md.
Rules enforced here:
- train/validation/test are disjoint by group_id; rows sharing a group or an identical normalized
  player text are one connected group when splits are assigned;
- only role=player texts become features; IDs, groups, splits, provenance, labels and
  reported_player quotes never do;
- vocabulary/IDF and classifiers are fitted on train only; model choice and the acceptance
  threshold use validation only; test is never read for fitting or selection;
- by default only approved human labels are trainable; synthetic rows need --smoke-synthetic and
  produce an artifact marked smoke_test_only that the service refuses without an explicit opt-in;
- frozen dev/control inputs are checked for exact text overlap and cannot become training data.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from functools import partial
import hashlib
import json
import platform
from pathlib import Path
import sys
import warnings

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from support_copilot import category_model as runtime
from support_copilot.schemas import TriageRequest

PROTOCOL = 'category-holdout-v1'
TRAINER_VERSION = 'category-trainer-v1'
SPLITS = ('train', 'validation', 'test')
KINDS = ('report', 'complaint')
PROVENANCES = ('human', 'human_adjudicated', 'assistant_silver', 'synthetic', 'feedback_unconfirmed')
HUMAN_PROVENANCES = frozenset({'human', 'human_adjudicated'})
ROOT = runtime.ROOT
DEFAULT_TAXONOMY = ROOT / 'data' / 'handout-2026-10-27' / 'taxonomy.json'
DEFAULT_FROZEN = ROOT / 'evaluation' / 'frozen-inputs.json'
FEATURE_SETS = {
    'word_1_2': ({'type': 'word', 'ngram': [1, 2]},),
    'char_wb_3_5': ({'type': 'char_wb', 'ngram': [3, 5]},),
    'word_1_2+char_wb_3_5': ({'type': 'word', 'ngram': [1, 2]}, {'type': 'char_wb', 'ngram': [3, 5]}),
}
CLASSIFIERS = (('linear_svc', 0.3), ('linear_svc', 1.0), ('linear_svc', 3.0),
               ('logistic_regression', 1.0), ('logistic_regression', 10.0))
CLASS_WEIGHTS = (None, 'balanced')


class DataError(ValueError):
    """Aggregated contract violations; messages hold codes and IDs only, never texts."""

    def __init__(self, problems: list[dict]):
        super().__init__('; '.join(problem['code'] for problem in problems))
        self.problems = problems


class LabeledExample(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    example_id: str = Field(min_length=1, max_length=200)
    group_id: str = Field(min_length=1, max_length=200)
    split: Literal['train', 'validation', 'test'] | None = None
    request: TriageRequest
    category: str = Field(min_length=3, max_length=100)
    label_provenance: Literal['human', 'human_adjudicated', 'assistant_silver', 'synthetic', 'feedback_unconfirmed']
    approved_for_training: bool


# --- Inputs ---------------------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def short_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def load_taxonomy(path: Path) -> dict:
    raw = path.read_bytes()
    data = json.loads(raw)
    categories = {}
    for item in data['categories']:
        code, kind = item['code'], item['kind']
        if kind not in KINDS or not code.startswith(kind + '.') or code in categories:
            raise ValueError('Invalid taxonomy category')
        categories[code] = {'kind': kind}
    human_always = set(data.get('needs_human_always', []))
    if not human_always <= categories.keys():
        raise ValueError('Unknown mandatory escalation category')
    return {'categories': categories, 'human_always': human_always, 'sha256': hashlib.sha256(raw).hexdigest()}


def read_rows(path: Path) -> list[dict]:
    """Parse and schema-check every line; report all failures by line number."""
    rows, problems = [], []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            example = LabeledExample.model_validate_json(line)
        except ValidationError as exc:
            fields = sorted({'.'.join(str(part) for part in error['loc'][:1]) for error in exc.errors()})
            problems.append({'code': 'invalid_row', 'line': number, 'fields': fields})
            continue
        rows.append({**example.model_dump(), '_line': number})
    if problems:
        raise DataError(problems)
    return rows


def text_key(request) -> str:
    return ' '.join(runtime.preprocess(runtime.player_text(request)).split())


def frozen_text_keys(manifest: Path) -> tuple[set[str], str]:
    """Keys of player texts in frozen evaluation sets, if those private files are present locally.

    Paths in the manifest are relative to the project root, i.e. the manifest's grandparent
    (evaluation/frozen-inputs.json).
    """
    if not manifest.exists():
        return set(), 'skipped_manifest_absent'
    root = manifest.resolve().parent.parent
    keys, present, missing = set(), 0, 0
    for item in json.loads(manifest.read_text(encoding='utf-8-sig')):
        path = root / item['path']
        if not item['path'].endswith('.jsonl'):
            continue
        if not path.exists():
            missing += 1
            continue
        present += 1
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row.get('request'), dict):
                    keys.add(text_key(row['request']))
    if not present:
        return keys, 'skipped_files_absent'
    return keys, 'checked' if not missing else f'partially_checked_{missing}_missing'


def trainable(row: dict, smoke: bool) -> bool:
    if smoke:
        return row['label_provenance'] in HUMAN_PROVENANCES | {'synthetic'}
    return row['label_provenance'] in HUMAN_PROVENANCES and row['approved_for_training'] is True


def validate(rows: list[dict], taxonomy: dict, *, frozen_keys: set[str] = frozenset(),
             require_splits: bool = True) -> dict:
    """Contract checks shared by every command. Raises DataError; returns counts and warnings."""
    problems, warnings_ = [], []
    ids = Counter(row['example_id'] for row in rows)
    duplicates = sorted(key for key, count in ids.items() if count > 1)
    if duplicates:
        problems.append({'code': 'duplicate_example_id', 'example_ids': duplicates[:50]})
    categories = taxonomy['categories']
    unknown = [row['example_id'] for row in rows if row['category'] not in categories]
    if unknown:
        problems.append({'code': 'category_not_in_taxonomy', 'example_ids': unknown[:50]})
    mismatch = [row['example_id'] for row in rows
                if row['category'] in categories and categories[row['category']]['kind'] != row['request']['kind']]
    if mismatch:
        problems.append({'code': 'category_kind_mismatch', 'example_ids': mismatch[:50]})
    empty = [row['example_id'] for row in rows if not runtime.player_text(row['request']).strip()]
    if empty:
        problems.append({'code': 'no_player_text', 'example_ids': empty[:50]})
    if require_splits:
        missing = [row['example_id'] for row in rows if row['split'] is None]
        if missing:
            problems.append({'code': 'split_missing', 'example_ids': missing[:50]})
    group_splits = defaultdict(set)
    for row in rows:
        if row['split']:
            group_splits[row['group_id']].add(row['split'])
    crossing = sorted(group for group, splits in group_splits.items() if len(splits) > 1)
    if crossing:
        problems.append({'code': 'group_in_several_splits', 'group_ids': crossing[:50]})
    by_key = defaultdict(list)
    for row in rows:
        by_key[(row['request']['kind'], text_key(row['request']))].append(row)
    conflicts, cross_split, repeated = [], [], 0
    for same in by_key.values():
        if len(same) < 2:
            continue
        repeated += len(same) - 1
        if len({row['category'] for row in same}) > 1:
            conflicts.append(sorted(row['example_id'] for row in same))
        if len({row['split'] for row in same if row['split']}) > 1:
            cross_split.append(sorted(row['example_id'] for row in same))
    if conflicts:
        problems.append({'code': 'conflicting_labels_for_same_text', 'example_id_sets': conflicts[:50]})
    if cross_split:
        problems.append({'code': 'same_text_in_several_splits', 'example_id_sets': cross_split[:50]})
    if repeated:
        warnings_.append({'code': 'repeated_texts_same_label', 'extra_rows': repeated})
    overlap = sorted(row['example_id'] for row in rows if text_key(row['request']) in frozen_keys)
    if overlap:
        problems.append({'code': 'overlaps_frozen_evaluation_inputs', 'example_ids': overlap[:50]})
    if problems:
        raise DataError(problems)
    counts = defaultdict(Counter)
    for row in rows:
        counts[row['split'] or 'unassigned'][row['category']] += 1
    return {'n': len(rows), 'warnings': warnings_,
            'by_split': {split: dict(sorted(counter.items())) for split, counter in sorted(counts.items())},
            'provenance': dict(Counter(row['label_provenance'] for row in rows)),
            'approved_for_training': sum(row['approved_for_training'] for row in rows)}


# --- Group-aware split assignment -----------------------------------------------------------------

def connected_groups(rows: list[dict]) -> list[str]:
    """Union of group_id and identical normalized text: near-copies never straddle splits."""
    parent = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for row in rows:
        a, b = find('g:' + row['group_id']), find('t:' + row['request']['kind'] + ':' + text_key(row['request']))
        if a != b:
            parent[max(a, b)] = min(a, b)
    return [find('g:' + row['group_id']) for row in rows]


def assign_splits(rows: list[dict], *, seed: int, test_fraction: float,
                  validation_fraction: float) -> tuple[list[dict], dict]:
    """Split connected groups: stratified by category when classes allow it, else seeded group shuffle."""
    import random
    from sklearn.model_selection import StratifiedGroupKFold

    if any(row['split'] for row in rows):
        raise ValueError('Rows already carry splits; refusing to reshuffle an existing holdout')
    if not 0 <= test_fraction < .5 or not 0 < validation_fraction < .5:
        raise ValueError('Fractions out of range')
    components = connected_groups(rows)
    labels = [row['category'] for row in rows]
    assigned = ['train'] * len(rows)
    methods = {}

    def take(indices, fraction, name, salt):
        folds = max(2, round(1 / fraction))
        groups = [components[i] for i in indices]
        if len(set(groups)) < folds:
            raise ValueError(f'Too few groups for a {name} split')
        try:
            splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=seed + salt)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)  # rare classes: sklearn warns, splitting still works
                _, chosen = next(splitter.split(indices, [labels[i] for i in indices], groups))
            picked = {indices[i] for i in chosen}
            methods[name] = f'stratified_group_kfold_1_of_{folds}'
        except ValueError:
            # Every class is smaller than the fold count: stratification is impossible, keep groups whole.
            unique = sorted(set(groups))
            random.Random(seed + salt).shuffle(unique)
            chosen_groups = set(unique[:max(1, round(len(unique) / folds))])
            picked = {i for i in indices if components[i] in chosen_groups}
            methods[name] = f'seeded_group_shuffle_1_of_{folds}_unstratified'
        for index in picked:
            assigned[index] = name
        return [i for i in indices if i not in picked]

    remaining = list(range(len(rows)))
    if test_fraction:
        remaining = take(remaining, test_fraction, 'test', 0)
    take(remaining, validation_fraction / (1 - test_fraction), 'validation', 1)
    result = []
    for row, split in zip(rows, assigned):
        clean = {key: value for key, value in row.items() if key != '_line'}
        result.append({**clean, 'split': split})
    return result, {'seed': seed, 'methods': methods, 'connected_groups': len(set(components))}


# --- Fitting and export ---------------------------------------------------------------------------

def _raw_analyzer(document: str, analyzer: dict) -> list[str]:
    return runtime.analyze(runtime.preprocess(document), analyzer)


def candidates() -> list[dict]:
    items = []
    for features in FEATURE_SETS:
        for classifier, c in CLASSIFIERS:
            for weight in CLASS_WEIGHTS:
                items.append({'name': f'{features}|{classifier}|C={c}|class_weight={weight or "none"}',
                              'features': features, 'classifier': classifier, 'C': c, 'class_weight': weight})
    return items


def fit(candidate: dict, texts: list[str], labels: list[str], seed: int):
    import numpy as np
    from scipy.sparse import hstack
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import LinearSVC

    vectorizers = [TfidfVectorizer(analyzer=partial(_raw_analyzer, analyzer=analyzer), sublinear_tf=True,
                                   norm='l2', smooth_idf=True, min_df=1, dtype=np.float64)
                   for analyzer in FEATURE_SETS[candidate['features']]]
    matrix = hstack([vectorizer.fit_transform(texts) for vectorizer in vectorizers]).tocsr()
    if candidate['classifier'] == 'linear_svc':
        model = LinearSVC(C=candidate['C'], class_weight=candidate['class_weight'], random_state=seed, max_iter=20000)
    else:
        model = LogisticRegression(C=candidate['C'], class_weight=candidate['class_weight'], random_state=seed,
                                   solver='lbfgs', max_iter=5000)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        model.fit(matrix, labels)
    return vectorizers, model, sum('converge' in str(item.message).lower() for item in caught)


def transform(vectorizers, texts):
    from scipy.sparse import hstack
    return hstack([vectorizer.transform(texts) for vectorizer in vectorizers]).tocsr()


def export_kind(candidate: dict, vectorizers, model, threshold, extra: dict) -> dict:
    blocks = []
    for analyzer, vectorizer in zip(FEATURE_SETS[candidate['features']], vectorizers):
        vocabulary = [None] * len(vectorizer.vocabulary_)
        for feature, index in vectorizer.vocabulary_.items():
            vocabulary[index] = feature
        blocks.append({'analyzer': dict(analyzer), 'vocabulary': vocabulary, 'sublinear_tf': True,
                       'norm': 'l2', 'idf': [float(value) for value in vectorizer.idf_]})
    return {'vectorizer_blocks': blocks,
            'classifier': {'type': candidate['classifier'], 'classes': [str(c) for c in model.classes_],
                           'coef': [[float(v) for v in row] for row in model.coef_],
                           'intercept': [float(v) for v in model.intercept_],
                           'C': candidate['C'], 'class_weight': candidate['class_weight']},
            'score': {'name': runtime.SCORE_NAMES[candidate['classifier']], 'threshold': threshold,
                      'selected_on': 'validation'},
            'selected_candidate': candidate['name'], **extra}


def metrics(actual, predicted, classes) -> dict:
    from support_copilot.measure import classification_metrics
    if not actual:
        return {'n': 0}
    value = classification_metrics(actual, predicted, classes)
    return {key: value[key] for key in ('n', 'correct', 'accuracy', 'macro_f1_all', 'macro_f1_present', 'absent', 'per_class')}


def brief(value: dict) -> dict:
    return {key: value.get(key) for key in ('n', 'correct', 'accuracy', 'macro_f1_all', 'macro_f1_present')}


def rule_predictions(rows: list[dict]) -> list[tuple[str, float, bool, dict]]:
    from support_copilot.service import rule_category
    return [rule_category(TriageRequest.model_validate(row['request'])) for row in rows]


def choose_threshold(scored: list[tuple[str, float]], rules: list[tuple[str, float, bool, dict]], actual: list[str],
                     classes: list[str], mandatory: frozenset) -> tuple[float | None, list[dict]]:
    """Maximize validation accuracy of the runtime hybrid; ties prefer macro-F1, then fewer changes."""
    from support_copilot.measure import classification_metrics
    options = [None, 0.0] + sorted({round(score, 12) for _, score in scored})
    table = []
    for threshold in options:
        hybrid = [runtime.combine(category, score, threshold, rule[0], appeal=rule[2], mandatory=mandatory,
                                  vetoes=rule[3])[0]
                  for (category, score), rule in zip(scored, rules)]
        value = classification_metrics(actual, hybrid, classes)
        changed = sum(h != r[0] for h, r in zip(hybrid, rules))
        table.append({'threshold': threshold, 'accuracy': value['accuracy'], 'macro_f1_all': value['macro_f1_all'],
                      'changed_vs_rules': changed})
    best = max(table, key=lambda row: (row['accuracy'], row['macro_f1_all'], -row['changed_vs_rules']))
    return best['threshold'], table


def dependency_versions() -> dict:
    import numpy
    import scipy
    import sklearn
    return {'python': platform.python_version(), 'platform': platform.platform(),
            'scikit-learn': sklearn.__version__, 'numpy': numpy.__version__, 'scipy': scipy.__version__}


def train(data: Path, taxonomy_path: Path, output: Path, *, seed: int = 20261031, smoke: bool = False,
          accuracy_tolerance: float = .02, frozen_manifest: Path = DEFAULT_FROZEN) -> dict:
    from support_copilot.measure import implementation_hash

    taxonomy = load_taxonomy(taxonomy_path)
    rows = read_rows(data)
    frozen_keys, frozen_status = frozen_text_keys(frozen_manifest)
    summary = validate(rows, taxonomy, frozen_keys=frozen_keys)
    excluded = Counter()
    usable = {'train': [], 'validation': []}
    for row in rows:
        if row['split'] == 'test':
            continue  # Counted in the summary only: never fitted, never used for selection.
        if trainable(row, smoke):
            usable[row['split']].append(row)
        else:
            excluded[f"{row['split']}:{row['label_provenance']}:approved={row['approved_for_training']}"] += 1
    mandatory = runtime.MANDATORY_RULE_CATEGORIES | frozenset(taxonomy['human_always'])
    kinds, skipped, report_kinds = {}, {}, {}
    for kind in KINDS:
        train_rows = [row for row in usable['train'] if row['request']['kind'] == kind]
        validation_rows = [row for row in usable['validation'] if row['request']['kind'] == kind]
        classes = sorted(code for code, item in taxonomy['categories'].items() if item['kind'] == kind)
        if len({row['category'] for row in train_rows}) < 2:
            skipped[kind] = 'fewer_than_two_train_classes'
            continue
        if not validation_rows:
            skipped[kind] = 'no_validation_rows'
            continue
        texts = [runtime.player_text(row['request']) for row in train_rows]
        labels = [row['category'] for row in train_rows]
        validation_texts = [runtime.player_text(row['request']) for row in validation_rows]
        actual = [row['category'] for row in validation_rows]
        rules = rule_predictions(validation_rows)
        rules_metrics = metrics(actual, [rule[0] for rule in rules], classes)
        results = []
        for index, candidate in enumerate(candidates()):
            vectorizers, model, convergence = fit(candidate, texts, labels, seed)
            predicted = [str(value) for value in model.predict(transform(vectorizers, validation_texts))]
            value = metrics(actual, predicted, classes)
            results.append({'order': index, 'candidate': candidate, 'validation': brief(value),
                            'convergence_warnings': convergence})
        best_accuracy = max(item['validation']['accuracy'] for item in results)
        eligible = [item for item in results if item['validation']['accuracy'] >= best_accuracy - accuracy_tolerance]
        chosen = max(eligible, key=lambda item: (item['validation']['macro_f1_all'], item['validation']['accuracy'],
                                                 -item['order']))
        candidate = chosen['candidate']
        vectorizers, model, _ = fit(candidate, texts, labels, seed)
        provisional = export_kind(candidate, vectorizers, model, 0.0, {})
        exported_model = runtime._kind_model(kind, provisional)
        scored = [exported_model.predict(text) for text in validation_texts]
        sklearn_predicted = [str(value) for value in model.predict(transform(vectorizers, validation_texts))]
        if [category for category, _ in scored] != sklearn_predicted:
            raise RuntimeError('Exported inference disagrees with scikit-learn; artifact not written')
        threshold, threshold_table = choose_threshold(scored, rules, actual, classes, mandatory)
        hybrid = [runtime.combine(category, score, threshold, rule[0], appeal=rule[2], mandatory=mandatory,
                                  vetoes=rule[3])[0]
                  for (category, score), rule in zip(scored, rules)]
        selection = {
            'rule': f'max validation macro_f1_all among candidates within {accuracy_tolerance} of best validation accuracy',
            'validation_rules_only': brief(rules_metrics),
            'validation_model_only': brief(metrics(actual, sklearn_predicted, classes)),
            'validation_hybrid_at_threshold': brief(metrics(actual, hybrid, classes)),
            'validation_is_optimistic': 'model and threshold were both selected on this validation split',
            'threshold_search': threshold_table,
        }
        absent = sorted(set(classes) - set(labels))
        kinds[kind] = export_kind(candidate, vectorizers, model, threshold, {
            'train_counts': dict(sorted(Counter(labels).items())),
            'validation_counts': dict(sorted(Counter(actual).items())),
            'taxonomy_classes_absent_from_train': absent,
        })
        report_kinds[kind] = {'selected': candidate['name'], 'threshold': threshold, 'selection': selection,
                              'candidates': [{key: item[key] for key in ('candidate', 'validation', 'convergence_warnings')}
                                             for item in results]}
    if not kinds:
        raise DataError([{'code': 'nothing_trainable', 'kinds': skipped}])
    used = usable['train'] + usable['validation']
    snapshot = hashlib.sha256(runtime.canonical(sorted(
        [row['example_id'], row['split'], row['category'], row['label_provenance'], text_key(row['request'])]
        for row in used)).encode()).hexdigest()
    body = {
        'purpose': 'smoke_test_only' if smoke else 'candidate',
        'trainer': TRAINER_VERSION,
        'preprocessing': {'version': runtime.PREPROCESSING_VERSION,
                          'source': 'role=player messages joined by newline; safety_view; urls/nicknames/digits masked'},
        'taxonomy_sha256': taxonomy['sha256'],
        'kinds': kinds,
        'training': {
            'seed': seed, 'accuracy_tolerance': accuracy_tolerance, 'dependencies': dependency_versions(),
            'implementation_sha256': implementation_hash(),
            'data': {'file_sha256': sha256_file(data), 'used_rows_snapshot_sha256': snapshot,
                     'summary': summary, 'used': {split: len(items) for split, items in usable.items()},
                     'excluded': dict(sorted(excluded.items())),
                     'frozen_overlap_check': frozen_status,
                     'trainable_provenance': sorted(HUMAN_PROVENANCES | ({'synthetic'} if smoke else set())),
                     'requires_approved_for_training': not smoke},
            'leakage_guards': {
                'group_digests': sorted({short_digest(row['group_id']) for row in used}),
                'text_digests': sorted({short_digest(text_key(row['request'])) for row in used})},
            'skipped_kinds': skipped,
            'quality_claim': 'none: synthetic smoke artifact' if smoke else 'validation only; final holdout pending',
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    digest = runtime.write_artifact(body, output / 'model.json')
    report = {'model_sha256': digest, 'purpose': body['purpose'], 'kinds': report_kinds,
              'skipped_kinds': skipped, 'data': body['training']['data'],
              'dependencies': body['training']['dependencies']}
    (output / 'training-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


# --- Versioned holdout evaluation -----------------------------------------------------------------

def evaluate(model_path: Path, data: Path, taxonomy_path: Path, *, split: str = 'test',
             allow_non_human: bool = False, frozen_manifest: Path = DEFAULT_FROZEN) -> dict:
    """Same metric definitions as measure.classification_metrics over all taxonomy categories."""
    from support_copilot.measure import implementation_hash
    from support_copilot.quality import bootstrap

    taxonomy = load_taxonomy(taxonomy_path)
    model = runtime.load_model(model_path)
    if model.taxonomy_sha256 != taxonomy['sha256']:
        raise DataError([{'code': 'taxonomy_mismatch'}])
    rows = read_rows(data)
    frozen_keys, frozen_status = frozen_text_keys(frozen_manifest)
    validate(rows, taxonomy, frozen_keys=frozen_keys)
    rows = [row for row in rows if row['split'] == split]
    if not rows:
        raise DataError([{'code': 'empty_evaluation_split', 'split': split}])
    non_human = [row['example_id'] for row in rows if row['label_provenance'] not in HUMAN_PROVENANCES]
    if non_human and not allow_non_human:
        raise DataError([{'code': 'non_human_labels_in_holdout', 'example_ids': non_human[:50]}])
    body = json.loads(model_path.read_text(encoding='utf-8'))['body']
    guards = body['training']['leakage_guards']
    leaked_groups = sorted(row['example_id'] for row in rows if short_digest(row['group_id']) in set(guards['group_digests']))
    leaked_texts = sorted(row['example_id'] for row in rows
                          if short_digest(text_key(row['request'])) in set(guards['text_digests']))
    if leaked_groups or leaked_texts:
        raise DataError([{'code': 'holdout_overlaps_training', 'by_group': leaked_groups[:50], 'by_text': leaked_texts[:50]}])
    mandatory = runtime.MANDATORY_RULE_CATEGORIES | frozenset(taxonomy['human_always'])
    classes = list(taxonomy['categories'])
    actual = [row['category'] for row in rows]
    rules = rule_predictions(rows)
    model_only, hybrid, sources = [], [], Counter()
    for row, rule in zip(rows, rules):
        kind_model = model.kinds.get(row['request']['kind'])
        if kind_model is None:
            model_only.append(rule[0])
            hybrid.append(rule[0])
            sources['kind_not_trained'] += 1
            continue
        category, score = kind_model.predict(runtime.player_text(row['request']))
        model_only.append(category)
        chosen, why = runtime.combine(category, score, kind_model.threshold, rule[0], appeal=rule[2], mandatory=mandatory,
                                      vetoes=rule[3])
        hybrid.append(chosen)
        sources[why] += 1
    rule_labels = [rule[0] for rule in rules]
    variants = {'rules': rule_labels, 'model_only': model_only, 'hybrid_runtime': hybrid}
    report = {
        'protocol': PROTOCOL, 'split': split, 'n': len(rows),
        'labels_provenance': dict(Counter(row['label_provenance'] for row in rows)),
        'labels_are_human_gold': not non_human,
        'model_sha256': model.sha256, 'model_purpose': model.purpose, 'data_sha256': sha256_file(data),
        'taxonomy_sha256': taxonomy['sha256'], 'implementation_sha256': implementation_hash(),
        'frozen_overlap_check': frozen_status,
        'metric_definition': 'support_copilot.measure.classification_metrics; macro_f1_all over all taxonomy categories',
        'variants': {name: {**metrics(actual, predicted, classes),
                            'accuracy_ci': bootstrap([a == p for a, p in zip(actual, predicted)]),
                            'by_kind': {kind: brief(metrics([a for a, r in zip(actual, rows) if r['request']['kind'] == kind],
                                                           [p for p, r in zip(predicted, rows) if r['request']['kind'] == kind],
                                                           [c for c in classes if taxonomy['categories'][c]['kind'] == kind]))
                                        for kind in KINDS}}
                     for name, predicted in variants.items()},
        'hybrid_vs_rules': {
            'changed': sum(h != r for h, r in zip(hybrid, rule_labels)),
            'fixed': sum(r != a and h == a for h, r, a in zip(hybrid, rule_labels, actual)),
            'broken': sum(r == a and h != a for h, r, a in zip(hybrid, rule_labels, actual)),
            'wrong_to_other_wrong': sum(r != a and h != a and h != r for h, r, a in zip(hybrid, rule_labels, actual)),
            'decision_sources': dict(sorted(sources.items())),
            'mandatory_rule_category_overridden': sum(r in mandatory and h != r for h, r in zip(hybrid, rule_labels)),
        },
        'not_measured_here': ['needs_human / escalation recall (no such labels in this contract)',
                              'priority and language', 'HTTP latency'],
        'limitations': ['Bootstrap CI is conditional on this sample.',
                        'Only valid as a blind estimate if the holdout was never used for any choice.'],
    }
    if model.purpose != 'candidate' or non_human:
        report['limitations'].insert(0, 'Not a quality estimate on players: smoke artifact or non-human labels.')
    return report


def _write_json(value: dict, path: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'assign-splits', 'train', 'evaluate'):
        command = sub.add_parser(name)
        command.add_argument('--data', type=Path, required=True)
        command.add_argument('--taxonomy', type=Path, default=DEFAULT_TAXONOMY)
        command.add_argument('--frozen-manifest', type=Path, default=DEFAULT_FROZEN)
        if name == 'assign-splits':
            command.add_argument('--output', type=Path, required=True)
            command.add_argument('--seed', type=int, default=20261031)
            command.add_argument('--test-fraction', type=float, default=.2)
            command.add_argument('--validation-fraction', type=float, default=.15)
        if name == 'train':
            command.add_argument('--output', type=Path, required=True, help='Directory for model.json and training-report.json')
            command.add_argument('--seed', type=int, default=20261031)
            command.add_argument('--accuracy-tolerance', type=float, default=.02)
            command.add_argument('--smoke-synthetic', action='store_true',
                                 help='Allow synthetic labels; artifact is marked smoke_test_only')
        if name == 'evaluate':
            command.add_argument('--model', type=Path, required=True)
            command.add_argument('--split', choices=SPLITS, default='test')
            command.add_argument('--output', type=Path)
            command.add_argument('--allow-non-human', action='store_true',
                                 help='Report on non-human labels; the report says it is not human gold')
        if name == 'validate':
            command.add_argument('--allow-unassigned-splits', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            taxonomy = load_taxonomy(args.taxonomy)
            frozen_keys, status = frozen_text_keys(args.frozen_manifest)
            result = validate(read_rows(args.data), taxonomy, frozen_keys=frozen_keys,
                              require_splits=not args.allow_unassigned_splits)
            result['frozen_overlap_check'] = status
            _write_json({'status': 'ok', **result}, None)
        elif args.command == 'assign-splits':
            taxonomy = load_taxonomy(args.taxonomy)
            rows = read_rows(args.data)
            frozen_keys, _ = frozen_text_keys(args.frozen_manifest)
            validate(rows, taxonomy, frozen_keys=frozen_keys, require_splits=False)
            if args.output.exists():
                raise ValueError('Output exists; choose a new file')
            result, info = assign_splits(rows, seed=args.seed, test_fraction=args.test_fraction,
                                         validation_fraction=args.validation_fraction)
            validate([{**row, '_line': 0} for row in result], taxonomy, frozen_keys=frozen_keys)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in result), encoding='utf-8')
            _write_json({'status': 'ok', 'output': str(args.output), **info,
                         'by_split': dict(Counter(row['split'] for row in result))}, None)
        elif args.command == 'train':
            report = train(args.data, args.taxonomy, args.output, seed=args.seed, smoke=args.smoke_synthetic,
                           accuracy_tolerance=args.accuracy_tolerance, frozen_manifest=args.frozen_manifest)
            _write_json({'status': 'ok', 'model_sha256': report['model_sha256'], 'purpose': report['purpose'],
                         'selected': {kind: value['selected'] for kind, value in report['kinds'].items()},
                         'thresholds': {kind: value['threshold'] for kind, value in report['kinds'].items()},
                         'validation': {kind: {name: value['selection'][name] for name in
                                               ('validation_rules_only', 'validation_model_only', 'validation_hybrid_at_threshold')}
                                        for kind, value in report['kinds'].items()},
                         'skipped_kinds': report['skipped_kinds']}, None)
        else:
            if args.model.exists() and json.loads(args.model.read_text(encoding='utf-8')).get('body', {}).get('purpose') == 'smoke_test_only':
                import os
                os.environ[runtime.ENV_ALLOW_SMOKE] = '1'  # Evaluating, not serving; the report is labelled.
            _write_json(evaluate(args.model, args.data, args.taxonomy, split=args.split,
                                 allow_non_human=args.allow_non_human, frozen_manifest=args.frozen_manifest), args.output)
    except DataError as exc:
        _write_json({'status': 'error', 'problems': exc.problems}, None)
        return 2
    except runtime.ModelUnavailable as exc:
        _write_json({'status': 'error', 'problems': [{'code': exc.code}]}, None)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print('Category training failed: ' + type(exc).__name__ + (': ' + str(exc) if isinstance(exc, ValueError) else ''),
              file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
