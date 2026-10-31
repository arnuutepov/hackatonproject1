"""Signed server-to-server ingress. No player-controlled operator privileges."""
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, StringConstraints, model_validator

from support_copilot.connector import fingerprint
from support_copilot.knowledge import get_knowledge
from support_copilot.operator import ShortText
from support_copilot.operator_store import OperatorStore, timestamp
from support_copilot.schemas import ContractModel, TriageRequest
from support_copilot.service import triage

router = APIRouter(prefix='/api/v1/game')


def game_store(request: Request):
    actor = getattr(request.state, 'game_actor', None)
    if actor is None:
        raise HTTPException(401, 'Signed game request required')
    return OperatorStore(actor=actor)


class GameTicket(ContractModel):
    event_id: UUID = Field(strict=False)
    player_id: int = Field(ge=1, le=2**53 - 1)
    title: ShortText
    channel: Literal['launcher', 'in_game']
    request: TriageRequest

    @model_validator(mode='after')
    def channel_contract(self):
        if self.request.kind != ('complaint' if self.channel == 'launcher' else 'report'):
            raise ValueError('Channel and request kind disagree')
        return self


class GameMessage(ContractModel):
    event_id: UUID = Field(strict=False)
    player_id: int = Field(ge=1, le=2**53 - 1)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


@router.post('/tickets', status_code=201)
def ingest(payload: GameTicket, response: Response, workspace=Depends(game_store)):
    digest = fingerprint({'actor': workspace.actor, **payload.model_dump(mode='json')})
    key = workspace.inbox_replay(str(payload.event_id), digest)
    replayed = key is not None
    if not replayed:
        started = perf_counter()
        analysis = triage(payload.request, use_configured_provider=False)
        context = {'is_mock': False, 'provider': 'signed_game_server', 'player_id': payload.player_id,
                   'server_actor': workspace.actor,
                   'profile': None, 'lookup_status': 'not_requested', 'snapshot_at': timestamp(),
                   'notice': 'Подписанное событие игрового сервера. Профиль и транзакции не запрашивались.'}
        key, replayed = workspace.ingest(str(payload.event_id), digest, payload.title, payload.request.model_dump(),
                                        payload.channel, context, analysis.model_dump(),
                                        round((perf_counter() - started)*1000, 2), get_knowledge().hashes,
                                        source='game_connector')
    response.status_code = 200 if replayed else 201
    return {'ticket_id': key, 'replayed': replayed, 'accepted': True}


@router.post('/tickets/{ticket_id}/messages')
def player_message(ticket_id: UUID, payload: GameMessage, workspace=Depends(game_store)):
    digest = fingerprint({'actor': workspace.actor, **payload.model_dump(mode='json')})
    replayed = workspace.game_message(str(ticket_id), str(payload.event_id), digest, payload.player_id, payload.text)
    return {'accepted': True, 'replayed': replayed, 'ticket_id': str(ticket_id)}
