import json

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.cli import predict
from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage

client = TestClient(app, client=('127.0.0.1', 40000), base_url='http://127.0.0.1')
PAYLOAD = {"kind": "complaint", "messages": [{"role": "player", "text": "Меня оскорбили", "attachments": 2}]}


def test_http_and_batch_match_shared_service(tmp_path):
    source, output = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text(json.dumps(PAYLOAD, ensure_ascii=False) + "\n", encoding="utf-8")
    assert predict(source, output) == 1
    response = client.post("/triage", json=PAYLOAD)
    assert response.status_code == 200
    expected = triage(TriageRequest.model_validate(PAYLOAD)).model_dump()
    assert response.json() == json.loads(output.read_text(encoding='utf-8')) == expected
    TriageResponse.model_validate(response.json())


def test_baseline_does_not_claim_to_inspect_attachments_or_generate():
    result = client.post("/triage", json=PAYLOAD).json()
    assert result["needs_human"] is True
    assert result["draft_available"] is True
    assert result['draft_reply']['provenance'] == 'safe_template'
    assert result["extracted"]["has_evidence"] is False
    assert result["draft_reply"]["cited"] == []


@pytest.mark.parametrize("payload", [
    {"kind": "report", "messages": [{"role": "admin", "text": "PRIVATE_ADMIN_TEXT"}]},
    {"kind": "invalid", "messages": PAYLOAD["messages"]},
    {"kind": "report", "messages": []},
    {"kind": "report", "messages": [{"role": "player", "text": "  "}]},
    {"kind": "report", "messages": [{"role": "player", "text": "test", "attachments": -1}]},
])
def test_invalid_or_label_leaking_requests_rejected(payload):
    response = client.post("/triage", json=payload)
    assert response.status_code == 422
    assert "PRIVATE_ADMIN_TEXT" not in response.text


@pytest.mark.parametrize('attachments', [None, 0, '2'])
def test_input_metadata_is_ignored_and_attachment_counts_are_normalized(attachments):
    payload = {'kind': 'report', 'verdict': 'PRIVATE_VERDICT', 'id': 'opaque',
               'messages': [{'text': 'Помогите, игра вылетает', 'attachments': attachments,
                             'punishment': 'PRIVATE_PUNISHMENT'}]}
    request = TriageRequest.model_validate(payload)
    assert request.messages[0].role == 'player'
    assert request.messages[0].attachments == (int(attachments) if attachments is not None else 0)
    assert 'PRIVATE_' not in request.model_dump_json()
    response = client.post('/triage', json=payload)
    assert response.status_code == 200
    assert 'PRIVATE_' not in response.text


def test_bad_line_does_not_abort_batch(tmp_path):
    source, output = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text(json.dumps(PAYLOAD) + "\ninvalid\n", encoding="utf-8")
    output.write_text("previous", encoding="utf-8")
    assert predict(source, output) == 2
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["needs_human"] is True
    assert "invalid_input_line" in rows[1]["reasons"]
    assert not list(tmp_path.glob(".predict-*"))


def test_raw_organizer_ticket_keeps_id_and_drops_admin(tmp_path):
    raw = {"ticket_id": "t-1", "kind": "report", "images_count": 1, "messages": [
        {"role": "player", "text": "Застрял в текстурах"},
        {"role": "admin", "text": "private admin answer"}]}
    source, output = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
    assert predict(source, output) == 1
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["ticket_id"] == "t-1"
    assert "private admin answer" not in json.dumps(row, ensure_ascii=False)


def test_input_cannot_be_overwritten(tmp_path):
    source = tmp_path / "in.jsonl"
    source.write_text("original")
    with pytest.raises(ValueError, match="different"):
        predict(source, source)
    assert source.read_text() == "original"


def test_health_identifies_baseline():
    assert client.get("/health").json()['mode'] == 'wave_5_measured_local'
