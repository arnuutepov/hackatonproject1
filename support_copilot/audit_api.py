"""Audit chain HTTP surface. Integrity is readable by any operator, anchoring by an admin."""
from fastapi import APIRouter, Depends, HTTPException, Query

from support_copilot.audit_chain import AuditChain, ChainBroken
from support_copilot.security import operator, require_roles

router = APIRouter(prefix='/audit', dependencies=[Depends(operator)])


@router.get('/verify')
def verify():
    from support_copilot.auth_store import AuthStore
    report = AuditChain().verify()
    with AuthStore().connect() as db:
        report['identity_outbox_pending'] = db.execute('SELECT count(*) FROM identity_chain_outbox WHERE delivered=0').fetchone()[0]
    return report


@router.get('/blocks')
def blocks(limit: int = Query(50, ge=1, le=200), before: int | None = Query(None, ge=1)):
    chain = AuditChain()
    head = chain.head()
    return {'height': head.index if head else -1, 'head_hash': head.block_hash if head else None,
            'blocks': [block.public() for block in chain.blocks(limit=limit, before=before)]}


@router.post('/checkpoint', status_code=201)
def checkpoint(principal=Depends(require_roles('admin'))):
    try:
        return AuditChain().checkpoint()
    except ChainBroken:
        raise HTTPException(409, 'Chain is broken or empty; anchor refused') from None
