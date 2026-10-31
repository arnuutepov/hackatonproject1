"""Prepare blinded human ratings and validate returned CSV without inventing scores."""
import argparse
from collections import Counter,defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import statistics
import unicodedata
import zipfile
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from support_copilot.measure import ROOT,CONTROL,load_control,sha
from support_copilot.schemas import TriageRequest,TriageResponse
from support_copilot.service import triage
from support_copilot.knowledge import get_knowledge

DRAFT_HEADERS = ['ID','Обращение','Черновик','Цитаты ответа','Язык обращения','Корректность 0–3',
                 'Полезность 0–3','Язык ответа 0–3','Обещания?','Заявляет просмотр?','Владение языком','Рецензент','Комментарий']
RULE_HEADERS = ['ID ссылки','ID обращения','Обращение','Источник','Текст пункта','Релевантность 0–2',
                'Владение языком','Рецензент','Комментарий']
SCORES = DRAFT_HEADERS[4:]
LANGUAGES = ['ru','kk','uz','en','es','pt','uk','other','und']
YES_NO = ['Нет','Да','Не уверен']
FLUENCY = ['Носитель','Свободно','Недостаточно']


def dump(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def context_text(request: TriageRequest) -> str:
    parts = ['Форма обращения: '+request.kind]
    for message in request.messages:
        role = 'Игрок' if message.role == 'player' else 'Другой участник'
        parts.append(f'{role}: {message.text}\nЗаявлено вложений: {message.attachments}')
    return '\n\n'.join(parts)


def prepare(output: Path,predictions: Path) -> dict:
    if output.exists():
        raise ValueError('Review output already exists; do not replace a distributed packet')
    rows = load_control()
    predicted = [json.loads(line) for line in predictions.read_text(encoding='utf-8').splitlines()]
    by_id = {row['ticket_id']:TriageResponse.model_validate(row['response']) for row in predicted}
    if len(by_id) != len(predicted) or set(by_id) != {row['ticket_id'] for row in rows}:
        raise ValueError('Prediction IDs do not match frozen control')
    sources = get_knowledge()
    draft_cards,rule_cards,supplement,private = [],[],[],{}
    order = rows.copy()
    random.Random(20261030).shuffle(order)
    for index,row in enumerate(order):
        result = by_id[row['ticket_id']]
        identifier = 'R-'+hashlib.sha256(('wave5:'+row['ticket_id']).encode()).hexdigest()[:10]
        request = TriageRequest.model_validate(row['request'])
        card = dict(zip(DRAFT_HEADERS,[identifier,context_text(request),result.draft_reply.text,
                    ', '.join(result.draft_reply.cited) or 'Нет цитат']+['']*9))
        draft_cards.append(card)
        private[identifier] = dict(ticket_id=row['ticket_id'],population='real_control',
                                   label_language_hint=row['lang'],response_hash=hashlib.sha256(result.model_dump_json().encode()).hexdigest())
        # These are search candidates, not citations asserted by the draft.
        for rank,candidate in enumerate(result.retrieved_rules[:3],1):
            if candidate.source_id not in sources.rules:
                raise ValueError('Unknown retrieved source')
            rule_cards.append(dict(zip(RULE_HEADERS,[identifier+f'-S{rank}',identifier,card['Обращение'],
                candidate.source_id,sources.rules[candidate.source_id].text,'','','',''])))
    synthetic = json.loads((ROOT/'evaluation/wave3-cases.json').read_text(encoding='utf-8'))
    for index,case in enumerate(item for item in synthetic if item['lang'] == 'kk'):
        request = TriageRequest(kind=case['kind'],messages=[{'role':'player','text':case['text']}])
        result = triage(request,use_configured_provider=False,use_verified_templates=True)
        identifier = 'K-'+hashlib.sha256(('wave5-synthetic:'+case['id']).encode()).hexdigest()[:10]
        supplement.append(dict(zip(DRAFT_HEADERS,[identifier,context_text(request),result.draft_reply.text,
                                                  ', '.join(result.draft_reply.cited) or 'Нет цитат']+['']*9)))
        private[identifier] = dict(population='synthetic_kk',case=case['id'],label_language_hint='kk')
    share = output/'reviewers'
    secret = output/'coordinator'
    share.mkdir(parents=True)
    secret.mkdir()
    public = dict(drafts=draft_cards,rules=rule_cards,supplement=supplement)
    dump(share/'cards.json',public)
    for filename,cards,headers in [('drafts.csv',draft_cards,DRAFT_HEADERS),('rules.csv',rule_cards,RULE_HEADERS),
                                   ('synthetic-kk.csv',supplement,DRAFT_HEADERS)]:
        with (share/filename).open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=headers)
            writer.writeheader()
            writer.writerows(cards)
    dump(secret/'key.json',private)
    dump(secret/'expected-cards.json',public)
    report = dict(real_cases=len(draft_cards),search_candidates=len(rule_cards),synthetic_kk_cases=len(supplement),
                  hinted_real_languages=dict(Counter(row['lang'] for row in rows)),
                  status='awaiting_human_ratings',rated_cases=0,
                  control_sha256=sha(ROOT/CONTROL),predictions_sha256=sha(predictions),
                  distribution='Share reviewers directory only. Do not share coordinator mapping or model labels.')
    dump(secret/'manifest.json',report)
    return report


def read_csv(path: Path,headers: list[str]):
    text = path.read_text(encoding='utf-8-sig')
    for delimiter in [',',';','\t']:
        try:
            records = list(csv.reader(io.StringIO(text),delimiter=delimiter,quotechar='"',doublequote=True,strict=True))
        except csv.Error:
            continue
        for index,row in enumerate(records[:12]):
            if row[:len(headers)] == headers:
                data = records[index+1:]
                if any(len(row)>len(headers) and any(row[len(headers):]) for row in data):
                    raise ValueError('Unexpected review columns')
                return [dict(zip(headers,(row+['']*len(headers))[:len(headers)])) for row in data if any(row)]
    raise ValueError('Review CSV header not found')


def normalized(text):
    return unicodedata.normalize('NFC',text).replace('\r\n','\n').replace('\r','\n')


def read_xlsx(path: Path,sheet_name: str,headers: list[str]) -> list[dict]:
    """Read values only; do not execute formulas, macros, links or network requests."""
    ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    relns='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(path) as archive:
        def xml(name):
            if archive.getinfo(name).file_size>12_000_000:
                raise ValueError('Review XML exceeds size limit')
            try:
                return ET.fromstring(archive.read(name))
            except DefusedXmlException:
                raise ValueError('Unsafe XML in review workbook') from None
        book=xml('xl/workbook.xml')
        sheet=next((node for node in book.findall('m:sheets/m:sheet',ns) if node.attrib['name']==sheet_name),None)
        if sheet is None:
            raise ValueError('Expected review sheet is missing')
        rels=xml('xl/_rels/workbook.xml.rels')
        target=next(node.attrib['Target'] for node in rels if node.attrib['Id']==sheet.attrib[f'{{{relns}}}id'])
        target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
        if '..' in target.split('/'):
            raise ValueError('Invalid workbook relationship')
        strings=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings=[''.join(item.text or '' for item in node.findall('.//m:t',ns)) for node in xml('xl/sharedStrings.xml').findall('m:si',ns)]
        rows=[]
        for row in xml(target).findall('m:sheetData/m:row',ns):
            values=['']*len(headers)
            for cell in row.findall('m:c',ns):
                letters=''.join(char for char in cell.attrib['r'] if char.isalpha())
                index=0
                for char in letters:index=index*26+ord(char)-64
                if index>len(headers):continue
                value=cell.find('m:v',ns)
                text=value.text or '' if value is not None else ''
                if cell.attrib.get('t')=='s':text=strings[int(text)]
                elif cell.attrib.get('t')=='inlineStr':text=''.join(item.text or '' for item in cell.findall('m:is//m:t',ns))
                values[index-1]=text
            rows.append(values)
        for index,row in enumerate(rows):
            if row==headers:
                return [dict(zip(headers,values)) for values in rows[index+1:] if any(values)]
        raise ValueError('Review workbook header not found')


def summarize_draft_ratings(returned: list[dict],expected: list[dict],key: dict) -> dict:
    original = {row['ID']:row for row in expected}
    seen,complete,pending = set(),[],0
    for row in returned:
        identifier = row['ID']
        if identifier not in original or identifier in seen:
            raise ValueError('Unknown or duplicate review ID')
        seen.add(identifier)
        if any(normalized(row[field]) != normalized(original[identifier][field]) for field in DRAFT_HEADERS[:4]):
            raise ValueError('Original review context or draft was changed')
        values = [row[field].strip() for field in SCORES[:-1]]
        if not any(values):
            pending += 1
            continue
        if not all(values):
            raise ValueError('Partially filled rating row')
        if row['Язык обращения'] not in LANGUAGES or row['Владение языком'] not in FLUENCY:
            raise ValueError('Invalid language or fluency')
        if any(row[field] not in {'0','1','2','3','NA'} for field in DRAFT_HEADERS[5:8]):
            raise ValueError('Invalid score; use 0..3 or NA')
        if any(row[field] not in YES_NO for field in DRAFT_HEADERS[8:10]):
            raise ValueError('Invalid promise or screenshot rating')
        complete.append((row,key[identifier]['population']))
    populations = {}
    for population in ['real_control','synthetic_kk']:
        selected = [row for row,origin in complete if origin == population]
        def mean(field):
            scores = [int(row[field]) for row in selected if row[field] != 'NA']
            return dict(rated=len(scores),mean=statistics.mean(scores) if scores else None)
        populations[population] = dict(completed=len(selected),
            correctness=mean('Корректность 0–3'),usefulness=mean('Полезность 0–3'),language=mean('Язык ответа 0–3'),
            promise_flags=sum(row['Обещания?']=='Да' for row in selected),
            screenshot_claim_flags=sum(row['Заявляет просмотр?']=='Да' for row in selected),
            uncertain_promises=sum(row['Обещания?']=='Не уверен' for row in selected),
            reviewer_languages=dict(Counter(row['Язык обращения'] for row in selected)),
            reviewer_fluency=dict(Counter(row['Владение языком'] for row in selected)))
    return dict(status='pending' if not complete else 'partial' if len(complete)<len(expected) else 'complete',
                expected=len(expected),completed=len(complete),not_submitted=len(expected)-len(seen),
                populations=populations,reviewers=sorted({row['Рецензент'] for row,_ in complete}))


def summarize_rule_ratings(returned,expected):
    originals = {row['ID ссылки']:row for row in expected}
    seen,scores = set(),[]
    for row in returned:
        identifier = row['ID ссылки']
        if identifier not in originals or identifier in seen:
            raise ValueError('Unknown or duplicate source-review ID')
        seen.add(identifier)
        if any(normalized(row[field]) != normalized(originals[identifier][field]) for field in RULE_HEADERS[:5]):
            raise ValueError('Source review context was changed')
        fields = [row[field].strip() for field in RULE_HEADERS[5:8]]
        if not any(fields):
            continue
        if not all(fields) or row['Релевантность 0–2'] not in {'0','1','2','NA'} or row['Владение языком'] not in FLUENCY:
            raise ValueError('Invalid or incomplete source review')
        if row['Релевантность 0–2'] != 'NA':
            scores.append(int(row['Релевантность 0–2']))
    return dict(expected=len(expected),rated=len(scores),relevant=sum(value==2 for value in scores),
                relevant_rate_among_rated=sum(value==2 for value in scores)/len(scores) if scores else None,
                mean=statistics.mean(scores) if scores else None,
                coverage=len(scores)/len(expected) if expected else None)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command',required=True)
    create = commands.add_parser('prepare')
    create.add_argument('--output',type=Path,required=True)
    create.add_argument('--predictions',type=Path,required=True)
    validate = commands.add_parser('import')
    validate.add_argument('--packet',type=Path,required=True)
    inputs=validate.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--drafts',type=Path)
    inputs.add_argument('--xlsx',type=Path)
    validate.add_argument('--rules',type=Path)
    validate.add_argument('--supplement',type=Path)
    validate.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        print(json.dumps(prepare(args.output,args.predictions),ensure_ascii=False))
    else:
        expected = json.loads((args.packet/'coordinator/expected-cards.json').read_text(encoding='utf-8'))
        key = json.loads((args.packet/'coordinator/key.json').read_text(encoding='utf-8'))
        returned = read_xlsx(args.xlsx,'Ответы',DRAFT_HEADERS) if args.xlsx else read_csv(args.drafts,DRAFT_HEADERS)
        cards = expected['drafts']
        if args.supplement or args.xlsx:
            returned += read_xlsx(args.xlsx,'Доп KK',DRAFT_HEADERS) if args.xlsx else read_csv(args.supplement,DRAFT_HEADERS)
            cards += expected['supplement']
        report = dict(drafts=summarize_draft_ratings(returned,cards,key),
                      rules=summarize_rule_ratings(read_xlsx(args.xlsx,'Поиск',RULE_HEADERS) if args.xlsx else read_csv(args.rules,RULE_HEADERS),expected['rules']) if args.rules or args.xlsx else None)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        dump(args.output,report)
        print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
