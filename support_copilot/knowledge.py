"""Validated local source index. Bare point numbers are deliberately not unique."""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from collections import Counter

DATA = Path(__file__).resolve().parent.parent / 'data' / 'handout-2026-10-27'


def normalize(text: str) -> str:
    return unicodedata.normalize('NFC', text).casefold().replace('ё', 'е')


def tokens(text: str) -> list[str]:
    # Small, transparent prefix matcher, not a language-specific stemmer.
    return [word[:6] for word in re.findall(r'[^\W\d_]{3,}', normalize(text))]


@dataclass(frozen=True)
class Rule:
    section_id: str
    section: str
    ref: str | None
    text: str

    @property
    def source_id(self) -> str:
        return f'{self.section_id}:{self.ref}' if self.ref else f'{self.section_id}:section'


class Knowledge:
    def __init__(self, directory: Path = DATA):
        raw = {name: (directory / f'{name}.json').read_bytes() for name in ('taxonomy', 'rules')}
        self.hashes = {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()}
        self.taxonomy = json.loads(raw['taxonomy'])
        self.categories = {}
        for category in self.taxonomy['categories']:
            code = category['code']
            if code in self.categories or category['kind'] not in ('report', 'complaint'):
                raise ValueError('Invalid or duplicate category')
            if not code.startswith(category['kind'] + '.'):
                raise ValueError('Category kind mismatch')
            self.categories[code] = category
        self.priorities = {item['code']: item for item in self.taxonomy['priorities']}
        if set(self.priorities) != {'p0', 'p1', 'p2', 'p3'}:
            raise ValueError('Invalid priorities')
        self.human_always = set(self.taxonomy['needs_human_always'])
        if not self.human_always <= self.categories.keys():
            raise ValueError('Unknown mandatory escalation category')
        self.rules = {}
        sections = set()
        for section in json.loads(raw['rules'])['rules']:
            sid = str(section['id'])
            if sid in sections:
                raise ValueError('Duplicate section')
            sections.add(sid)
            points = section.get('points') or [{'ref': None, 'text': section['text']}]
            for point in points:
                rule = Rule(sid, section['category'], point['ref'], point['text'])
                if not rule.text.strip() or rule.source_id in self.rules:
                    raise ValueError('Empty or duplicate rule')
                self.rules[rule.source_id] = rule
        self.words = {key: Counter(tokens(rule.section + ' ' + rule.text)) for key, rule in self.rules.items()}
        df = Counter(word for counts in self.words.values() for word in counts)
        self.idf = {word: math.log(1 + len(self.rules) / frequency) for word, frequency in df.items()}
        self.lengths = {key: sum(counts.values()) for key, counts in self.words.items()}
        self.avg_length = sum(self.lengths.values()) / max(1, len(self.lengths))
        self.grams = {key: self.chargrams(rule.section + ' ' + rule.text) for key, rule in self.rules.items()}
        # Taxonomy references remain advisory, never unquestioned evidence.
        self.category_references = {
            code: {ref: self.resolve(ref) for ref in category.get('rules', [])}
            for code, category in self.categories.items()
        }

    def resolve(self, reference: str, section_id: str | None = None) -> list[str]:
        ref = reference.strip()
        if ref in self.rules:
            return [ref]
        if ref.upper().startswith('ЖБ '):
            section_id, ref = '31', ref[3:].strip()
        if ref.upper() in ('GZ', 'ГЗ', 'ЗЗ'):
            return [key for key, rule in self.rules.items() if rule.section_id == '32']
        return [key for key, rule in self.rules.items()
                if rule.ref == ref and (section_id is None or rule.section_id == section_id)]

    @staticmethod
    def chargrams(text):
        text = re.sub(r'\s+', ' ', normalize(text))
        return {text[i:i+3] for i in range(max(0, len(text)-2)) if text[i:i+3].strip()}

    def search(self, text: str, limit: int = 5, *, mode='hybrid') -> list[dict]:
        expanded = normalize(text)
        for abbreviation, full in {'дм': 'убийство урон без причины', 'dm': 'убийство урон без причины',
                                   'гз': 'зеленая зона', 'gz': 'зеленая зона', 'зз': 'зеленая зона',
                                   'жб': 'жалобы', 'rmt': 'продажа реальные деньги'}.items():
            expanded = re.sub(r'\b' + abbreviation + r'\b', full, expanded)
        if mode == 'hybrid':
            for phrase, translation in {'haqorat': 'оскорбление', 'балағат': 'оскорбление',
                                        'cheat': 'читы', 'aldadi': 'обман', 'алдап': 'обман'}.items():
                expanded = expanded.replace(phrase, phrase + ' ' + translation)
        query = Counter(tokens(expanded))
        scored = []
        for key, counts in self.words.items():
            overlap = set(query) & set(counts)
            if not overlap:
                continue
            score = sum(self.idf[word] * (1 + math.log(counts[word])) for word in overlap)
            score /= 1 + .015 * sum(counts.values())
            scored.append((score, key))
        if mode == 'hybrid' and query:
            bm25 = []
            for key, counts in self.words.items():
                value = sum(self.idf[word] * counts[word] * 2.2 /
                            (counts[word] + 1.2*(.25 + .75*self.lengths[key]/self.avg_length))
                            for word in set(query) & counts.keys())
                if value: bm25.append((value, key))
            grams = self.chargrams(expanded)
            lexical_ids = {key for _, key in bm25}
            char = [(len(grams & doc) / math.sqrt(max(1, len(grams)*len(doc))), key)
                    for key, doc in self.grams.items() if grams & doc]
            char = [(score, key) for score, key in char if key in lexical_ids or score >= .25]
            fused = Counter()
            for ranking in (bm25, char):
                for rank, (_, key) in enumerate(sorted(ranking, key=lambda x: (-x[0],x[1]))[:30], 1):
                    fused[key] += 1/(60+rank)
            scored = [(score, key) for key, score in fused.items()]
        results = []
        for score, key in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]:
            rule = self.rules[key]
            results.append(dict(source_id=key, section_id=rule.section_id, section=rule.section,
                                ref=rule.ref, text=rule.text, score=round(score, 4)))
        return results


@lru_cache(maxsize=1)
def get_knowledge() -> Knowledge:
    try:
        return Knowledge()
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid knowledge structure') from exc
