"""Opt-in local TF-IDF + linear category backend: dependency-free inference and guarded selection.

The artifact is a JSON file produced by `support_copilot.category_training` on the team's machine.
No pickle/joblib is loaded, so inference needs neither scikit-learn nor numpy and an artifact cannot
execute code. The rule core in service.py stays the default (`COPILOT_CATEGORY_BACKEND=rules`).

Boundaries kept by design:
- Features come only from role=player texts (see `player_text`); reported_player quotes, admin
  outcomes, IDs, labels, group IDs, provenance and file names are never inputs.
- The model can change the category only. Mandatory payment/account categories, taxonomy
  `needs_human_always` categories and appeal decisions of the rule core are never overridden,
  and service.py keeps every escalation the rule category would have triggered.
- A linear score (SVM margin or uncalibrated logistic maximum) is not a probability of being
  correct. Its acceptance threshold is chosen on the validation split by the trainer.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys

from support_copilot.safety import safety_view

logger = logging.getLogger('uvicorn.error')

FORMAT = 'oguzki-category-tfidf-v1'
PREPROCESSING_VERSION = 'player-safety-view-v1'
BACKENDS = ('rules', 'shadow', 'tfidf')
ENV_BACKEND = 'COPILOT_CATEGORY_BACKEND'
ENV_MODEL = 'COPILOT_CATEGORY_MODEL'
ENV_ALLOW_SMOKE = 'COPILOT_CATEGORY_ALLOW_SMOKE'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = ROOT / 'outputs' / 'category-model' / 'current' / 'model.json'
MANDATORY_RULE_CATEGORIES = frozenset({'report.payment', 'report.account'})
SCORE_NAMES = {'linear_svc': 'svm_margin_top1_minus_top2_uncalibrated',
               'logistic_regression': 'logistic_max_probability_uncalibrated'}
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

# Masking keeps identities and numbers from becoming memorized features.
_URL = re.compile(r'https?://\S+|\bt\.me/\S+|\bdiscord\.gg/\S+')
_NICKNAME = re.compile(r'\b[a-z][a-z0-9]{1,24}_[a-z][a-z0-9_]{1,24}\b')
_DIGITS = re.compile(r'\d+')
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


# --- Shared preprocessing and analyzers (the trainer imports exactly these functions) -------------

def player_text(request) -> str:
    """The only feature source: the reporter's own messages. Accepts TriageRequest or its dict."""
    messages = request['messages'] if isinstance(request, dict) else request.messages
    texts = []
    for message in messages:
        role = message['role'] if isinstance(message, dict) else message.role
        if role == 'player':
            texts.append(message['text'] if isinstance(message, dict) else message.text)
    return '\n'.join(texts)


def preprocess(text: str) -> str:
    view = safety_view(text)
    view = _URL.sub(' urltoken ', view)
    view = _NICKNAME.sub(' nicktoken ', view)
    return _DIGITS.sub('0', view)


def words(view: str) -> list[str]:
    return _WORD.findall(view)


def word_ngrams(view: str, low: int, high: int) -> list[str]:
    tokens = words(view)
    features = []
    for size in range(low, high + 1):
        features.extend('w:' + ' '.join(tokens[index:index + size]) for index in range(len(tokens) - size + 1))
    return features


def char_wb_ngrams(view: str, low: int, high: int) -> list[str]:
    """Character n-grams inside space-padded words, like scikit-learn's char_wb analyzer."""
    features = []
    for token in words(view):
        padded = f' {token} '
        for size in range(low, high + 1):
            if len(padded) < size:
                break
            features.extend('c:' + padded[index:index + size] for index in range(len(padded) - size + 1))
    return features


def analyze(view: str, analyzer: dict) -> list[str]:
    low, high = analyzer['ngram']
    if analyzer['type'] == 'word':
        return word_ngrams(view, low, high)
    if analyzer['type'] == 'char_wb':
        return char_wb_ngrams(view, low, high)
    raise ValueError('Unknown analyzer')


# --- Artifact -------------------------------------------------------------------------------------

def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def body_digest(body: dict) -> str:
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def write_artifact(body: dict, path: Path) -> str:
    """Deterministic JSON artifact: identical inputs and seed give identical bytes."""
    digest = body_digest(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(canonical({'format': FORMAT, 'body_sha256': digest, 'body': body}) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return digest


class ModelUnavailable(Exception):
    """Carries a short diagnostic code only; never request text or file contents."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class KindModel:
    kind: str
    blocks: tuple            # ({'analyzer':…, 'index':{feature:i}, 'idf':[…], 'sublinear_tf':bool, 'offset':int}, …)
    classifier: str
    classes: tuple
    coef: tuple              # rows of floats over all blocks (one row for a binary problem)
    intercept: tuple
    threshold: float | None

    @property
    def score_name(self) -> str:
        return SCORE_NAMES[self.classifier]

    def features(self, text: str) -> dict[int, float]:
        view = preprocess(text)
        vector = {}
        for block in self.blocks:
            counts = {}
            for feature in analyze(view, block['analyzer']):
                index = block['index'].get(feature)
                if index is not None:
                    counts[index] = counts.get(index, 0) + 1
            values = {}
            for index, count in counts.items():
                tf = 1 + math.log(count) if block['sublinear_tf'] else float(count)
                values[index] = tf * block['idf'][index]
            norm = math.sqrt(sum(value * value for value in values.values()))
            if norm:
                for index, value in values.items():
                    vector[block['offset'] + index] = value / norm
        return vector

    def decision(self, text: str) -> list[float]:
        vector = self.features(text)
        return [sum(value * row[index] for index, value in vector.items()) + bias
                for row, bias in zip(self.coef, self.intercept)]

    def predict(self, text: str) -> tuple[str, float]:
        """Return (category, score). The score is only comparable with this model's threshold."""
        values = self.decision(text)
        if len(self.classes) == 2:
            value = values[0]
            label = self.classes[1] if value > 0 else self.classes[0]
            if self.classifier == 'logistic_regression':
                probability = 1 / (1 + math.exp(-value)) if value >= 0 else math.exp(value) / (1 + math.exp(value))
                return label, max(probability, 1 - probability)
            return label, abs(value)
        order = sorted(range(len(values)), key=lambda index: (-values[index], index))
        top = order[0]
        if self.classifier == 'logistic_regression':
            peak = values[top]
            exps = [math.exp(value - peak) for value in values]
            return self.classes[top], exps[top] / sum(exps)
        return self.classes[top], values[top] - values[order[1]]


@dataclass(frozen=True)
class CategoryModel:
    path: str
    sha256: str
    purpose: str
    taxonomy_sha256: str
    kinds: dict = field(default_factory=dict)
    selected: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {'path': self.path, 'model_sha256': self.sha256, 'purpose': self.purpose,
                'taxonomy_sha256': self.taxonomy_sha256,
                'kinds': {kind: {'classifier': model.classifier, 'classes': list(model.classes),
                                 'score': model.score_name, 'threshold': model.threshold,
                                 'features': sum(len(block['idf']) for block in model.blocks),
                                 'selected': self.selected.get(kind)}
                          for kind, model in sorted(self.kinds.items())}}


def _kind_model(kind: str, spec: dict) -> KindModel:
    offset = 0
    blocks = []
    for block in spec['vectorizer_blocks']:
        vocabulary = block['vocabulary']
        if len(vocabulary) != len(block['idf']) or len(set(vocabulary)) != len(vocabulary):
            raise ModelUnavailable('artifact_vocabulary_invalid')
        analyzer = block['analyzer']
        if analyzer.get('type') not in ('word', 'char_wb') or len(analyzer.get('ngram', ())) != 2:
            raise ModelUnavailable('artifact_analyzer_invalid')
        blocks.append({'analyzer': {'type': analyzer['type'], 'ngram': tuple(analyzer['ngram'])},
                       'index': {feature: index for index, feature in enumerate(vocabulary)},
                       'idf': tuple(float(value) for value in block['idf']),
                       'sublinear_tf': bool(block['sublinear_tf']), 'offset': offset})
        offset += len(vocabulary)
    classifier = spec['classifier']
    classes = tuple(classifier['classes'])
    rows = 1 if len(classes) == 2 else len(classes)
    coef = tuple(tuple(float(value) for value in row) for row in classifier['coef'])
    intercept = tuple(float(value) for value in classifier['intercept'])
    if (classifier['type'] not in SCORE_NAMES or len(classes) < 2 or len(set(classes)) != len(classes)
            or len(coef) != rows or len(intercept) != rows or any(len(row) != offset for row in coef)):
        raise ModelUnavailable('artifact_classifier_invalid')
    if any(not code.startswith(kind + '.') for code in classes):
        raise ModelUnavailable('artifact_kind_mismatch')
    # null: on validation the model never beat the rules, so it always abstains.
    threshold = spec['score']['threshold']
    threshold = None if threshold is None else float(threshold)
    if spec['score'].get('name') != SCORE_NAMES[classifier['type']] or spec['score'].get('selected_on') != 'validation':
        raise ModelUnavailable('artifact_threshold_invalid')
    if (not all(math.isfinite(value) for row in coef for value in row + intercept)
            or (threshold is not None and not math.isfinite(threshold))):
        raise ModelUnavailable('artifact_not_finite')
    return KindModel(kind, tuple(blocks), classifier['type'], classes, coef, intercept, threshold)


def load_model(path: Path) -> CategoryModel:
    try:
        size = path.stat().st_size
    except OSError:
        raise ModelUnavailable('artifact_missing') from None
    if size > MAX_ARTIFACT_BYTES:
        raise ModelUnavailable('artifact_too_large')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'),
                         parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    except (OSError, UnicodeError, ValueError):
        raise ModelUnavailable('artifact_unreadable') from None
    if not isinstance(raw, dict) or raw.get('format') != FORMAT or not isinstance(raw.get('body'), dict):
        raise ModelUnavailable('artifact_format_unknown')
    body = raw['body']
    try:
        digest = body_digest(body)
    except ValueError:
        raise ModelUnavailable('artifact_not_finite') from None
    if digest != raw.get('body_sha256'):
        raise ModelUnavailable('artifact_checksum_mismatch')
    purpose = body.get('purpose')
    if purpose not in ('candidate', 'smoke_test_only'):
        raise ModelUnavailable('artifact_purpose_unknown')
    if purpose == 'smoke_test_only' and os.environ.get(ENV_ALLOW_SMOKE) != '1':
        raise ModelUnavailable('smoke_model_not_allowed')
    if body.get('preprocessing', {}).get('version') != PREPROCESSING_VERSION:
        raise ModelUnavailable('artifact_preprocessing_mismatch')
    try:
        kinds = {kind: _kind_model(kind, spec) for kind, spec in body['kinds'].items()
                 if kind in ('report', 'complaint')}
        selected = {kind: spec.get('selected_candidate') for kind, spec in body['kinds'].items()}
        taxonomy = str(body['taxonomy_sha256'])
    except ModelUnavailable:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ModelUnavailable('artifact_structure_invalid') from None
    if not kinds:
        raise ModelUnavailable('artifact_has_no_kinds')
    return CategoryModel(str(path), digest, purpose, taxonomy, kinds, selected)


_CACHE: dict = {}


def cached_model(path: Path) -> CategoryModel:
    """Reload when the file changes; remember a failure code for the same file state."""
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, os.environ.get(ENV_ALLOW_SMOKE) == '1')
    except OSError:
        raise ModelUnavailable('artifact_missing') from None
    if key not in _CACHE:
        _CACHE.clear()
        try:
            _CACHE[key] = load_model(path)
        except ModelUnavailable as exc:
            logger.warning('Category model unavailable (%s); rule categories are used', exc.code)
            _CACHE[key] = exc
    value = _CACHE[key]
    if isinstance(value, ModelUnavailable):
        raise value
    return value


# --- Backend selection and guarded combination ----------------------------------------------------

def configured_backend() -> tuple[str, str | None]:
    value = os.environ.get(ENV_BACKEND, 'rules').strip().lower() or 'rules'
    if value not in BACKENDS:
        return 'rules', 'category_backend_unknown_value_rules_used'
    return value, None


def configured_model_path() -> Path:
    value = os.environ.get(ENV_MODEL, '').strip()
    return Path(value) if value else DEFAULT_MODEL


def combine(model_category: str, score: float, threshold: float | None, rule_category: str, *, appeal: bool,
            mandatory: frozenset, vetoes: dict | None = None) -> tuple[str, str]:
    """Single source of truth for the hybrid decision (runtime and trainer threshold search)."""
    if rule_category in mandatory:
        return rule_category, 'category_guard:mandatory_rule_category'
    if appeal:
        return rule_category, 'category_guard:appeal_rule'
    if vetoes and model_category in vetoes and model_category != rule_category:
        return rule_category, vetoes[model_category]
    if threshold is None or score < threshold:
        return rule_category, 'category_model_abstained:below_validation_threshold'
    return model_category, 'category_source:model'


@dataclass
class CategoryDecision:
    category: str
    certainty: float
    reasons: list[str] = field(default_factory=list)
    confidence_method: str | None = None


def decide(request, rule_category: str, rule_certainty: float, *, appeal: bool, categories: dict,
           human_always: set, taxonomy_sha256: str | None, vetoes: dict | None = None) -> CategoryDecision:
    """Return the category the service should report. The default backend changes nothing."""
    backend, problem = configured_backend()
    if backend == 'rules':
        return CategoryDecision(rule_category, rule_certainty, [problem] if problem else [])
    reasons = ['category_backend:' + backend]
    try:
        model = cached_model(configured_model_path())
        if taxonomy_sha256 is not None and model.taxonomy_sha256 != taxonomy_sha256:
            raise ModelUnavailable('taxonomy_mismatch')
        kind_model = model.kinds.get(request.kind)
        if kind_model is None:
            raise ModelUnavailable('kind_not_trained')
        predicted, score = kind_model.predict(player_text(request))
        if predicted not in categories or categories[predicted]['kind'] != request.kind:
            raise ModelUnavailable('model_category_not_in_taxonomy')
    except ModelUnavailable as exc:
        return CategoryDecision(rule_category, rule_certainty, reasons + ['category_model_unavailable:' + exc.code])
    mandatory = MANDATORY_RULE_CATEGORIES | frozenset(human_always)
    chosen, why = combine(predicted, score, kind_model.threshold, rule_category, appeal=appeal, mandatory=mandatory,
                          vetoes=vetoes)
    if backend == 'shadow':
        # Shadow keeps the rule decision and records the candidate for offline comparison.
        side = 'accepted' if why == 'category_source:model' else why.split(':', 1)[1]
        return CategoryDecision(rule_category, rule_certainty,
                                reasons + [f'category_shadow_candidate:{predicted}', 'category_shadow_would_be:' + side])
    reasons.append(why)
    if why != 'category_source:model':
        return CategoryDecision(rule_category, rule_certainty, reasons)
    if chosen == rule_category:
        reasons.append('category_model_agrees_with_rules')
    # Nominal level only, the same as a matched rule; never a calibrated probability.
    return CategoryDecision(chosen, max(rule_certainty, .5), reasons,
                            confidence_method='tfidf_linear_validation_threshold_uncalibrated')


def backend_metadata() -> dict:
    """Report metadata for measurement entry points. Never changes metric formulas."""
    backend, problem = configured_backend()
    info = {'backend': backend, 'config_problem': problem}
    if backend == 'rules':
        return info
    path = configured_model_path()
    try:
        model = cached_model(path)
        info.update(status='loaded', model_sha256=model.sha256, purpose=model.purpose,
                    taxonomy_sha256=model.taxonomy_sha256,
                    thresholds={kind: value.threshold for kind, value in model.kinds.items()})
    except ModelUnavailable as exc:
        info.update(status='unavailable', code=exc.code)
    info['model_file'] = path.name
    return info


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Check a local category model artifact without serving it.')
    parser.add_argument('command', choices=['check'])
    parser.add_argument('--model', type=Path, default=None, help=f'Defaults to ${ENV_MODEL} or {DEFAULT_MODEL}')
    args = parser.parse_args(argv)
    path = args.model or configured_model_path()
    try:
        model = load_model(path)
    except ModelUnavailable as exc:
        print(json.dumps({'status': 'unavailable', 'code': exc.code}), file=sys.stderr)
        return 2
    print(json.dumps({'status': 'ok', **model.summary()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
