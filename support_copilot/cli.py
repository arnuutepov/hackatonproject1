"""One request per JSONL line. Replace the output only after full success.

Accepts both the inference contract ({"kind", "messages"}) and the organizer's raw
ticket format (ticket_id, images_count, admin messages). ``ticket_id``/``id`` is
echoed into each output line. A malformed line never aborts the batch: it gets a
conservative response with ``needs_human=true``.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from pydantic import ValidationError

from support_copilot.schemas import TriageRequest
from support_copilot.service import triage

ID_FIELDS = ("ticket_id", "id")
PLAYER_ROLES = ("player", "reported_player")


def _attachments(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        return int(value)
    return 0


def _lenient_request(record: dict) -> TriageRequest:
    """Contract view of a raw ticket: drop admin replies and everything after them."""
    kind = record.get("kind")
    if kind not in ("complaint", "report"):
        kind = "complaint"
    images = _attachments(record.get("images_count", 0))
    messages = []
    for message in record.get("messages") or []:
        if isinstance(message, str):
            message = {"role": "player", "text": message}
        if not isinstance(message, dict):
            continue
        role = message.get("role", "player")
        if role == "admin":
            break
        if role not in PLAYER_ROLES:
            role = "player"
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        attachments = message.get("attachments", images if not messages and role == "player" else 0)
        messages.append({"role": role, "text": text[:20000], "attachments": _attachments(attachments)})
    if not messages and isinstance(record.get("text"), str) and record["text"].strip():
        messages.append({"role": "player", "text": record["text"][:20000], "attachments": images})
    return TriageRequest.model_validate({"kind": kind, "messages": messages[:100]})


def _request(record: dict) -> TriageRequest:
    try:
        return TriageRequest.model_validate(record)
    except ValidationError:
        pass
    if "ticket_id" in record:
        try:
            from support_copilot.dataset import adapt
            return adapt(record).request
        except Exception:
            pass
    return _lenient_request(record)


def _fallback(kind: str):
    request = TriageRequest.model_validate({"kind": kind, "messages": [{"role": "player", "text": "—"}]})
    response = triage(request)
    return response.model_copy(update={
        "needs_human": True,
        "confidence": 0.0,
        "confidence_level": "low",
        "reasons": list(response.reasons) + ["invalid_input_line"],
    })


def _line(line: str) -> str:
    record, ticket_id = None, None
    try:
        record = json.loads(line)
    except ValueError:
        pass
    if isinstance(record, dict):
        for field in ID_FIELDS:
            if record.get(field) is not None:
                ticket_id = record[field]
                break
    try:
        if not isinstance(record, dict):
            raise ValueError
        response = triage(_request(record))
    except Exception:
        kind = record.get("kind") if isinstance(record, dict) else None
        response = _fallback(kind if kind in ("complaint", "report") else "complaint")
    if ticket_id is None:
        return response.model_dump_json()
    payload = {"ticket_id": ticket_id, **response.model_dump(mode="json")}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def predict(source: Path, destination: Path) -> int:
    if source.resolve() == destination.resolve():
        raise ValueError("Input and output must be different files")
    temporary = None
    count = 0
    try:
        with source.open(encoding="utf-8-sig") as incoming:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix=".predict-", delete=False) as outgoing:
                temporary = Path(outgoing.name)
                for line in incoming:
                    if not line.strip():
                        continue
                    outgoing.write(_line(line) + "\n")
                    count += 1
        os.replace(temporary, destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Triage and guarded drafts from JSONL (contract or raw tickets)")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        count = predict(args.input, args.output)
    except (OSError, UnicodeError, ValueError) as exc:
        # Filesystem errors can include paths; never echo request contents.
        print('Prediction failed: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 2
    print(f"Wrote {count} predictions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
