"""Loopback-only operator API. No player delivery or production identity is claimed."""
import os
import sqlite3
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, StringConstraints

from support_copilot.knowledge import get_knowledge
from support_copilot.operator_store import AnalysisRequired, ConnectorRequired, HumanReviewRequired, MissingTicket, OperatorStore, RevisionConflict
from support_copilot.schemas import ContractModel, TriageRequest
from support_copilot.service import triage


from support_copilot.security import operator
from support_copilot.operator_assistant import AssistantQuestion


router = APIRouter(prefix='/operator', dependencies=[Depends(operator)])

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class NewTicket(ContractModel):
    title: ShortText
    request: TriageRequest


class AnalyzeTicket(ContractModel):
    expected_revision: int = Field(ge=1)


class Decision(AnalyzeTicket):
    draft: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    action: Literal['save', 'escalate']
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ''


class Workflow(AnalyzeTicket):
    action: Literal['take', 'release', 'resolve', 'reopen']
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ''


class ChatMessage(AnalyzeTicket):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


def store(principal=Depends(operator)) -> OperatorStore:
    return OperatorStore(actor=principal['id'])


def ticket_key(ticket_id: UUID) -> str:
    return str(ticket_id)


@router.get('/config')
def configuration(principal=Depends(operator)):
    knowledge = get_knowledge()
    return {
        'mode': 'operator_workspace', 'delivery_available': False, 'operator': principal,
        'provider': os.environ.get('COPILOT_DRAFT_PROVIDER', 'template'),
        'assistant_model': os.environ.get('COPILOT_CAPYBARA_MODEL', 'gemini-3.8-flash'),
        'taxonomy_version': knowledge.taxonomy['version'], 'knowledge_hashes': knowledge.hashes,
        'categories': {code: item['title'] for code, item in knowledge.categories.items()},
        'priorities': {code: item['title'] for code, item in knowledge.priorities.items()},
    }


@router.get('/tickets')
def tickets(workspace: OperatorStore = Depends(store)):
    return {'tickets': workspace.list()}


@router.post('/tickets', status_code=201)
def create_ticket(payload: NewTicket, workspace: OperatorStore = Depends(store)):
    key = workspace.create(payload.title, payload.request.model_dump())
    return workspace.get(key)


@router.post('/demo')
def add_examples(workspace: OperatorStore = Depends(store)):
    # Written from scratch. These are not copied/derived from organizer tickets.
    examples = [
        {'title': 'Не поступил платёж', 'request': {'kind': 'report', 'messages': [
            {'role': 'player', 'text': 'Оплатил донат, деньги списались, а баланс не изменился. Сервер 7.', 'attachments': 0}]}},
        {'title': 'Аккаунтқа кіру мәселесі', 'request': {'kind': 'report', 'messages': [
            {'role': 'player', 'text': 'Сәлеметсіз бе! Аккаунтыма кіре алмаймын. Құпиясөзім өзгерген сияқты.', 'attachments': 0}]}},
        {'title': 'Yordam kerak', 'request': {'kind': 'report', 'messages': [
            {'role': 'player', 'text': 'Salom! Men devor ichida qotib qoldim, yordam bering.', 'attachments': 0}]}},
        {'title': 'Противоречие в жалобе', 'request': {'kind': 'complaint', 'messages': [
            {'role': 'player', 'text': 'На сервере 7 игрок Demo_Pilot стрелял в меня в зелёной зоне. У меня есть скриншоты.', 'attachments': 2},
            {'role': 'reported_player', 'text': 'Я не начинал стрельбу. Он напал первым.', 'attachments': 0}]}},
        {'title': 'Неизвестный пункт правил', 'request': {'kind': 'complaint', 'messages': [
            {'role': 'player', 'text': 'Игрок Demo_Pilot оскорбил меня. Ссылаюсь на пункт 99.99.', 'attachments': 0}]}},
        {'title': 'Застрял в текстурах', 'request': {'kind': 'report', 'messages': [
            {'role': 'player', 'text': 'Застрял в текстурах', 'attachments': 0}]}},
    ]
    return {'created': workspace.seed(examples), 'count': len(examples)}


@router.get('/tickets/{ticket_id}')
def get_ticket(ticket_id: UUID, workspace: OperatorStore = Depends(store)):
    return workspace.get(ticket_key(ticket_id))


@router.post('/tickets/{ticket_id}/analyze')
def analyze_ticket(ticket_id: UUID, payload: AnalyzeTicket, workspace: OperatorStore = Depends(store)):
    key = ticket_key(ticket_id)
    before = workspace.get(key)
    if before['revision'] != payload.expected_revision:
        raise RevisionConflict()
    # Same inference entry point as /triage and make predict. No browser-supplied result is trusted.
    started = perf_counter()
    analysis = triage(TriageRequest.model_validate(before['payload']))
    duration = round((perf_counter() - started) * 1000, 2)
    workspace.analyze(key, payload.expected_revision, analysis.model_dump(), duration, get_knowledge().hashes)
    return workspace.get(key)


@router.post('/tickets/{ticket_id}/decision')
def decide(ticket_id: UUID, payload: Decision, workspace: OperatorStore = Depends(store)):
    key = ticket_key(ticket_id)
    workspace.decide(key, payload.expected_revision, payload.draft, payload.action, payload.note)
    return workspace.get(key)


@router.get('/rules/{source_id}')
def rule(source_id: str):
    knowledge = get_knowledge()
    item = knowledge.rules.get(source_id)
    if item is None:
        raise HTTPException(404, 'Rule not found')
    return {'source_id': item.source_id, 'section': item.section, 'section_id': item.section_id,
            'ref': item.ref, 'text': item.text, 'hash': knowledge.hashes['rules']}


@router.post('/tickets/{ticket_id}/workflow')
def workflow(ticket_id: UUID, payload: Workflow, workspace: OperatorStore = Depends(store), principal=Depends(operator)):
    key = ticket_key(ticket_id)
    ticket = workspace.get(key)
    senior = principal['role'] in {'lead', 'admin'}
    if payload.action in {'take', 'release'} and ticket['assigned_to'] not in {None, principal['id']} and not senior:
        raise HTTPException(403, 'Another operator owns this assignment')
    workspace.workflow(key, payload.expected_revision, payload.action, allow_human_resolution=senior, note=payload.note)
    return workspace.get(key)


@router.post('/tickets/{ticket_id}/chat')
def chat_message(ticket_id: UUID, payload: ChatMessage, workspace: OperatorStore = Depends(store)):
    key = ticket_key(ticket_id)
    workspace.add_chat_message(key, payload.expected_revision, 'admin', payload.text)
    return workspace.get(key)


@router.post('/tickets/{ticket_id}/assistant')
def assistant(ticket_id: UUID, payload: AssistantQuestion,
              workspace: OperatorStore = Depends(store)):
    from support_copilot.operator_assistant import answer
    if os.environ.get('COPILOT_DRAFT_PROVIDER') != 'capybara':
        raise HTTPException(503, 'Configure Capybara on the server first')
    return answer(workspace.get(ticket_key(ticket_id)), payload)


def install_handlers(app):
    @app.exception_handler(ConnectorRequired)
    async def connector_required(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': 'Create the ticket through the mock connector first'})

    @app.exception_handler(HumanReviewRequired)
    async def human_required(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': 'Mandatory human review cannot be resolved by this workspace'})

    @app.exception_handler(MissingTicket)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': 'Ticket not found'})

    @app.exception_handler(RevisionConflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': 'Ticket changed; reload before retrying'})

    @app.exception_handler(AnalysisRequired)
    async def not_analyzed(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': 'Analyze the ticket before saving a decision'})

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={'detail': 'Operator storage unavailable'})
