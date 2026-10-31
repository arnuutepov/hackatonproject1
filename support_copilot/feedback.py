"""Operator edits become review candidates, never automatic training labels."""
import argparse
import hashlib
import json
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS operator_feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL REFERENCES tickets(id),
 revision INTEGER NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL,
 original_draft TEXT NOT NULL, edited_draft TEXT NOT NULL,
 labels TEXT, reviewer TEXT, reviewed_at TEXT,
 UNIQUE(ticket_id,revision)
);
'''


def record(db,ticket,revision,actor,draft,at):
    analysis=json.loads(ticket['analysis'])
    original=analysis['draft_reply']['text']
    if draft!=original:
        db.execute('INSERT INTO operator_feedback(ticket_id,revision,created_at,actor,original_draft,edited_draft) VALUES (?,?,?,?,?,?)',
                   (ticket['id'],revision,at,actor,original,draft))


def approve(store, feedback_id, labels, reviewer):
    from support_copilot.knowledge import get_knowledge
    from support_copilot.operator_store import timestamp
    knowledge=get_knowledge()
    if labels['category'] not in knowledge.categories or labels['priority'] not in knowledge.priorities:
        raise ValueError('Labels outside taxonomy')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT f.*,t.payload FROM operator_feedback f JOIN tickets t ON t.id=f.ticket_id WHERE f.id=?',(feedback_id,)).fetchone()
        if row is None:raise ValueError('Unknown feedback')
        if row['reviewer'] is not None:
            if json.loads(row['labels']) == labels:
                return
            raise ValueError('Reviewed labels are immutable; create a new revision')
        if row['actor']==reviewer:raise ValueError('A second operator must review the edit')
        if knowledge.categories[labels['category']]['kind']!=json.loads(row['payload'])['kind']:
            raise ValueError('Category kind mismatch')
        db.execute('UPDATE operator_feedback SET labels=?,reviewer=?,reviewed_at=? WHERE id=?',
                   (json.dumps(labels),reviewer,timestamp(),feedback_id))


def export(store, output):
    # Default export deliberately excludes free text and identity. Join privately for annotation if required.
    with store.connect() as db:
        rows=db.execute('SELECT id,ticket_id,revision,labels,reviewed_at FROM operator_feedback WHERE labels IS NOT NULL AND reviewer IS NOT NULL ORDER BY id').fetchall()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('w',encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps({'case_id':hashlib.sha256(row['ticket_id'].encode()).hexdigest(),
                'revision':row['revision'],'labels':json.loads(row['labels']),'provenance':'operator_edit_second_review',
                'reviewed_at':row['reviewed_at'],'eligible_for_training':False},ensure_ascii=False)+'\n')
    output.chmod(0o600)
    return len(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from support_copilot.operator_store import OperatorStore
    print(json.dumps({'exported':export(OperatorStore(),args.output),'automatic_training':False}))

if __name__=='__main__':main()
