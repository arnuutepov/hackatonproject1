import json

from fastapi.testclient import TestClient
import pytest

from support_copilot.api import app
from support_copilot.cli import predict
from support_copilot.schemas import TriageRequest, TriageResponse
from support_copilot.service import triage

client = TestClient(app)
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
    {**PAYLOAD, "verdict": "approved"},
    {**PAYLOAD, "punishment": "мут"},
    {**PAYLOAD, "violation": "2.1"},
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


def test_bad_batch_preserves_previous_output(tmp_path):
    source, output = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    source.write_text(json.dumps(PAYLOAD) + "\ninvalid\n", encoding="utf-8")
    output.write_text("previous", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        predict(source, output)
    assert output.read_text() == "previous"
    assert not list(tmp_path.glob(".predict-*"))


def test_input_cannot_be_overwritten(tmp_path):
    source = tmp_path / "in.jsonl"
    source.write_text("original")
    with pytest.raises(ValueError, match="different"):
        predict(source, source)
    assert source.read_text() == "original"


def test_health_identifies_baseline():
    assert client.get("/health").json()['mode'] == 'wave_3_guarded_drafts'
