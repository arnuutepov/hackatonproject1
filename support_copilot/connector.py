"""Local mock adapter: separate game envelope from the unchanged inference contract."""
import hashlib
import json
import logging
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from pydantic import Field, StringConstraints, model_validator

from support_copilot.knowledge import get_knowledge
from support_copilot.mock_provider import SCENARIOS, player_context, scenario_request
from support_copilot.operator import AnalyzeTicket, ShortText, local_access, store
from support_copilot.operator_store import OperatorStore
from support_copilot.schemas import ContractModel, TriageRequest
from support_copilot.service import triage

router = APIRouter(prefix='/api/v1', dependencies=[Depends(local_access)])
logger = logging.getLogger('uvicorn.error')
Channel = Literal['game_chat', 'telegram']


def fingerprint(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


class SimulateTicket(ContractModel):
    event_id: UUID = Field(strict=False)
    scenario_id: Literal['payment_ru', 'account_kk_injection', 'stuck_uz_injection',
                         'complaint_ru', 'unknown_player'] | None = None
    title: ShortText | None = None
    player_id: int | None = Field(default=None, ge=1, le=2**53 - 1)
    channel: Channel = 'game_chat'
    request: TriageRequest | None = None

    @model_validator(mode='after')
    def one_source(self):
        if self.scenario_id is not None:
            if any(value is not None for value in (self.request, self.player_id, self.title)):
                raise ValueError('Scenario cannot be overridden')
        elif self.request is None or self.player_id is None or self.title is None:
            raise ValueError('Provide a scenario or title, player_id and request')
        return self


class MockAction(AnalyzeTicket):
    action_id: UUID = Field(strict=False)
    action: Literal['reply', 'escalate']
    draft: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ''
    channel: Channel = 'game_chat'


@router.get('/connector')
def connector_configuration():
    return {'mode': 'mock', 'real_delivery_available': False, 'enrichment_provider': 'mock_fixture_v1',
            'channels': ['game_chat', 'telegram'],
            'scenarios': [{'id': key, 'title': item['title'], 'player_id': item['player_id']}
                          for key, item in SCENARIOS.items()]}


@router.post('/simulate-ticket', status_code=201)
def simulate(payload: SimulateTicket, response: Response, workspace: OperatorStore = Depends(store)):
    source = payload.model_dump(mode='json')
    digest = fingerprint(source)
    # Retries are answered from the stored snapshot, without another analysis/model call.
    ticket_id = workspace.inbox_replay(str(payload.event_id), digest)
    replayed = ticket_id is not None
    if not replayed:
        example = SCENARIOS.get(payload.scenario_id)
        title = example['title'] if example else payload.title
        player_id = example['player_id'] if example else payload.player_id
        request = TriageRequest.model_validate(scenario_request(payload.scenario_id)) if example else payload.request
        started = perf_counter()
        # Mock ingestion is deterministic and never inherits a paid-provider opt-in.
        analysis = triage(request, use_configured_provider=False)
        duration = round((perf_counter() - started) * 1000, 2)
        ticket_id, replayed = workspace.ingest(str(payload.event_id), digest, title, request.model_dump(),
                                              payload.channel, player_context(player_id),
                                              analysis.model_dump(), duration, get_knowledge().hashes)
    response.status_code = 200 if replayed else 201
    return {'mode': 'mock', 'replayed': replayed, 'ticket': workspace.get(ticket_id)}


@router.post('/tickets/{ticket_id}/actions')
def outbound(ticket_id: UUID, payload: MockAction, workspace: OperatorStore = Depends(store)):
    event, replayed = workspace.mock_action(str(ticket_id), payload.model_dump(mode='json'),
                                             fingerprint(payload.model_dump(mode='json')))
    audit = workspace.export_mock_audit()
    if not replayed:
        logger.info('[MOCK OUTBOX] action=%s player_id=%s channel=%s action_id=%s delivered=false',
                    event['action'], event['player_id'], event['channel'], event['action_id'])
    return {'mode': 'mock', 'replayed': replayed, 'event': event, 'audit': audit,
            'ticket': workspace.get(str(ticket_id))}


@router.get('/outbox')
def outbox(workspace: OperatorStore = Depends(store)):
    return {'mode': 'mock', 'events': workspace.outbox(), 'audit': workspace.export_mock_audit()}
