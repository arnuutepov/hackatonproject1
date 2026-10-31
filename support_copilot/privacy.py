"""Best-effort redaction and explicit local operational-data retention. Not full anonymization."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import re

PATTERNS=[
 (re.compile(r'(?i)\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{15,}|Bearer\s+\S+)'),'[SECRET]'),
 (re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),'[EMAIL]'),
 (re.compile(r'(?<!\w)\+?\d[\d ()-]{9,}\d(?!\w)'),'[PHONE_OR_PAYMENT_ID]'),
 (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),'[IP]'),
 (re.compile(r'\b[A-Za-z][A-Za-z0-9]{1,24}_[A-Za-z][A-Za-z0-9_]{1,24}\b'),'[NICKNAME]'),
]


def redact(value):
    for pattern,replacement in PATTERNS:value=pattern.sub(replacement,value)
    return value


def redact_tree(value, field=None):
    if isinstance(value,str):
        if field == 'content':
            try:
                parsed=json.loads(value)
                if isinstance(parsed,(dict,list)):
                    return json.dumps(redact_tree(parsed),ensure_ascii=False)
            except ValueError:
                pass
        # Enums, source IDs and control fields must remain intact.
        return redact(value) if field in {'text','content','question','operator_question','answer','quote'} else PATTERNS[0][0].sub('[SECRET]',value)
    if isinstance(value,list):return [redact_tree(v,field) for v in value]
    if isinstance(value,dict):return {k:redact_tree(v,k) for k,v in value.items()}
    return value


def retain(store, days=30, *, apply=False):
    if not 1<=days<=3650:raise ValueError('Retention must be 1..3650 days')
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        ids=[r['id'] for r in db.execute("SELECT id FROM tickets WHERE status='resolved' AND updated_at < ?",(cutoff,))]
        if apply:
            db.execute('PRAGMA secure_delete=ON')
            for ticket in ids:
                for statement in (
                    'DELETE FROM events WHERE ticket_id=?',
                    'DELETE FROM assignments WHERE ticket_id=?',
                    'DELETE FROM chat_messages WHERE ticket_id=?',
                    'DELETE FROM connector_inbox WHERE ticket_id=?',
                    'DELETE FROM connector_outbox WHERE ticket_id=?',
                    'DELETE FROM human_reviews WHERE ticket_id=?',
                    'DELETE FROM game_messages WHERE ticket_id=?',
                    'DELETE FROM operator_feedback WHERE ticket_id=?',
                ):
                    db.execute(statement,(ticket,))
                db.execute('DELETE FROM tickets WHERE id=?',(ticket,))
    if apply:
        import sqlite3
        with sqlite3.connect(store.path) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
    return {'eligible_closed_tickets':len(ids),'deleted':len(ids) if apply else 0,'dry_run':not apply,
            'cutoff':cutoff,'chain_metadata_retained':True,'backups_erased':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30);p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    from support_copilot.operator_store import OperatorStore
    print(json.dumps(retain(OperatorStore(),args.days,apply=args.apply)))

if __name__=='__main__':main()
