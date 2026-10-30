"""One request per JSONL line. Replace the output only after full success."""
import argparse
import os
from pathlib import Path
import sys
import tempfile

from pydantic import ValidationError

from support_copilot.schemas import TriageRequest
from support_copilot.service import triage


def predict(source: Path, destination: Path) -> int:
    if source.resolve() == destination.resolve():
        raise ValueError("Input and output must be different files")
    temporary = None
    count = 0
    try:
        with source.open(encoding="utf-8") as incoming:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                             prefix=".predict-", delete=False) as outgoing:
                temporary = Path(outgoing.name)
                for number, line in enumerate(incoming, 1):
                    try:
                        request = TriageRequest.model_validate_json(line)
                    except ValidationError:
                        raise ValueError(f"Invalid request at line {number}; output unchanged") from None
                    outgoing.write(triage(request).model_dump_json() + "\n")
                    count += 1
        os.replace(temporary, destination)
        return count
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wave 3: triage and guarded drafts from inference-contract JSONL")
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
