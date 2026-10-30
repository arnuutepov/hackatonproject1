"""Local-only dataset audit, blind annotation sampling and leakage-aware splitting."""
import argparse
from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile

from pydantic import ValidationError

from support_copilot.normalization import KAZAKH_MAPPING_VERSION, normalize_text
from support_copilot.schemas import TriageRequest


class DatasetError(ValueError):
    """Safe error message: never include the contents of a ticket."""


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def records(path: Path):
    """Stream UTF-8 JSONL or JSONL.GZ, rejecting invalid lines rather than hiding them."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                raise DatasetError(f"Invalid JSON at line {number}") from None
            if not isinstance(value, dict):
                raise DatasetError(f"Expected JSON object at line {number}")
            yield number, value


def identifier(value, name: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"Invalid or missing {name}")
    return value


@dataclass
class Ticket:
    ticket_id: str
    reporter_hash: str | None
    request: TriageRequest
    original_messages: list[dict]
    duplicate_hash: str
    lang: str
    server_lang: str | None
    replacements: int
    removed_admin: bool
    dropped_after_admin: int


def adapt(record: dict, *, repair_kazakh: bool = False) -> Ticket:
    ticket_id = identifier(record.get("ticket_id"), "ticket_id", required=True)
    reporter = identifier(record.get("reporter_hash"), "reporter_hash")
    lang = record.get("lang") or "und"
    server_lang = record.get("server_lang")
    if not isinstance(lang, str) or (server_lang is not None and not isinstance(server_lang, str)):
        raise DatasetError("Invalid language metadata")
    messages = record.get("messages")
    if not isinstance(messages, list) or not messages:
        raise DatasetError("Invalid or missing messages")
    images = record.get("images_count", 0)
    if type(images) is not int or images < 0:
        raise DatasetError("Invalid images_count")
    incoming, originals = [], []
    removed_admin, dropped, replacements = False, 0, 0
    for message in messages:
        if not isinstance(message, dict):
            raise DatasetError("Invalid message object")
        role = message.get("role")
        if role == "admin":
            removed_admin = True
            continue
        if removed_admin:
            dropped += 1
            continue
        if role not in ("player", "reported_player"):
            raise DatasetError("Unsupported role before first administrator reply")
        text = message.get("text")
        if not isinstance(text, str):
            raise DatasetError("Invalid message text")
        normalized = normalize_text(text, lang=lang, server_lang=server_lang,
                                    repair_kazakh=repair_kazakh)
        replacements += normalized.replacements
        # Preserve submitted raw text separately; never include reference outcomes.
        attachments = message.get("attachments", images if not incoming and role == "player" else 0)
        incoming.append({"role": role, "text": normalized.text, "attachments": attachments})
        originals.append({"role": role, "text": text, "attachments": attachments})
    try:
        request = TriageRequest.model_validate({"kind": record.get("kind"), "messages": incoming})
    except ValidationError:
        raise DatasetError("Record does not satisfy inference contract") from None
    if not any(message.role == "player" for message in request.messages):
        raise DatasetError("No player message before administrator reply")
    # Exact duplicate of the observable player conversation, not administrator outcome.
    canonical = [(m.role, " ".join(m.text.casefold().split())) for m in request.messages]
    fingerprint = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()
    return Ticket(ticket_id, reporter, request, originals, fingerprint, lang, server_lang,
                  replacements, removed_admin, dropped)


class Groups:
    """Connect shared reporters and exact repeated conversations transitively."""
    def __init__(self):
        self.parent = []
        self.size = []

    def add(self) -> int:
        index = len(self.parent)
        self.parent.append(index)
        self.size.append(1)
        return index

    def root(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def join(self, left: int, right: int):
        left, right = self.root(left), self.root(right)
        if left == right:
            return
        if self.size[left] < self.size[right]:
            left, right = right, left
        self.parent[right] = left
        self.size[left] += self.size[right]


@dataclass
class Index:
    entries: list[dict]
    groups: Groups
    audit: dict


def scan(path: Path, *, repair_kazakh: bool = False) -> Index:
    entries, groups = [], Groups()
    ids, reporters, conversations = set(), {}, {}
    counts = {name: Counter() for name in ("kind", "lang", "server_lang", "category", "priority")}
    fields = Counter()
    totals = Counter()
    errors = []
    for line, raw in records(path):
        totals["records"] += 1
        for field in ("category", "priority", "verdict", "punishment", "violation"):
            if raw.get(field) is not None:
                fields[field] += 1
        try:
            ticket = adapt(raw, repair_kazakh=repair_kazakh)
        except DatasetError as exc:
            totals["invalid_records"] += 1
            if len(errors) < 20:
                errors.append({"line": line, "reason": str(exc)})
            continue
        if ticket.ticket_id in ids:
            totals["duplicate_ticket_ids"] += 1
        ids.add(ticket.ticket_id)
        index = groups.add()
        if ticket.reporter_hash:
            if ticket.reporter_hash in reporters:
                groups.join(index, reporters[ticket.reporter_hash])
            else:
                reporters[ticket.reporter_hash] = index
        else:
            totals["missing_reporter_hash"] += 1
        if ticket.duplicate_hash in conversations:
            groups.join(index, conversations[ticket.duplicate_hash])
            totals["exact_duplicate_conversations"] += 1
        else:
            conversations[ticket.duplicate_hash] = index
        entries.append({"ticket_id": ticket.ticket_id, "reporter_hash": ticket.reporter_hash,
                        "duplicate_hash": ticket.duplicate_hash, "lang": ticket.lang,
                        "kind": ticket.request.kind})
        counts["kind"][ticket.request.kind] += 1
        counts["lang"][ticket.lang] += 1
        counts["server_lang"][ticket.server_lang or "und"] += 1
        for name in ("category", "priority"):
            label = raw.get(name)
            if isinstance(label, str):
                counts[name][label] += 1
        totals["records_with_admin_removed"] += int(ticket.removed_admin)
        totals["messages_after_admin_removed"] += ticket.dropped_after_admin
        totals["kazakh_character_replacements"] += ticket.replacements
        totals["records_with_attachments"] += int(any(m.attachments for m in ticket.request.messages))
    group_sizes = Counter(groups.root(i) for i in range(len(entries)))
    report = {
        "schema_version": 1, "totals": dict(totals), "valid_records": len(entries),
        "distributions": {name: dict(value) for name, value in counts.items()},
        "non_null_reference_fields": dict(fields), "invalid_examples": errors,
        "connected_groups": len(group_sizes), "largest_group": max(group_sizes.values(), default=0),
        "normalization": {"repair_kazakh": repair_kazakh,
                          "mapping": KAZAKH_MAPPING_VERSION if repair_kazakh else None},
        "limitations": [
            "Language fields are source hints, not independently verified language labels.",
            "Reference verdict/punishment/violation are not category or priority ground truth.",
            "Exact conversation matches do not detect semantic paraphrases.",
            "Missing reporter hashes prevent guarantees about player disjointness.",
            "Grouping generic repeated messages may create very large components.",
            "Audit counts do not measure model quality; no model was run.",
        ],
    }
    return Index(entries, groups, report)


def split(index: Index, *, seed: int, control_ratio: float) -> dict[str, list[int]]:
    if not 0 < control_ratio < 1:
        raise DatasetError("control_ratio must be between 0 and 1")
    if index.audit["totals"].get("invalid_records") or index.audit["totals"].get("duplicate_ticket_ids"):
        raise DatasetError("Resolve invalid records and duplicate ticket IDs before preparation")
    components = {}
    for i in range(len(index.entries)):
        components.setdefault(index.groups.root(i), []).append(i)
    if len(components) < 2:
        raise DatasetError("Need at least two independent groups for dev/control split")
    # Stable for source reordering. Seeded hash over component membership, not root indices.
    ordered = sorted(components.values(), key=lambda members: hashlib.sha256(
        (str(seed) + "\0" + "\0".join(sorted(index.entries[i]["ticket_id"] for i in members))).encode()
    ).digest())
    target = len(index.entries) * control_ratio
    control, dev = [], []
    for members in ordered:
        if (len(control) + len(members) < len(index.entries)
                and abs(len(control) + len(members) - target) < abs(len(control) - target)):
            control.extend(members)
        else:
            dev.extend(members)
    if not control:
        closest = min(ordered, key=lambda members: abs(len(members) - target))
        control = list(closest)
        chosen = set(control)
        dev = [i for i in dev if i not in chosen]
    if not dev:
        raise DatasetError("Unable to form nonempty dev partition")
    return {"dev": dev, "control": control}


def balanced_sample(indices: list[int], entries: list[dict], size: int, seed: int) -> list[int]:
    """Round-robin language/kind strata; not a prevalence-representative benchmark."""
    buckets = {}
    for i in indices:
        entry = entries[i]
        buckets.setdefault((entry["lang"], entry["kind"]), []).append(i)
    rng = random.Random(seed)
    for bucket in buckets.values():
        bucket.sort(key=lambda i: entries[i]["ticket_id"])
        rng.shuffle(bucket)
    result = []
    while len(result) < size:
        changed = False
        for key in sorted(buckets):
            if buckets[key] and len(result) < size:
                result.append(buckets[key].pop())
                changed = True
        if not changed:
            break
    return result


def prepare(source: Path, destination: Path, *, sample_size: int = 100, seed: int = 42,
            control_ratio: float = .2, repair_kazakh: bool = False) -> dict:
    if destination.exists():
        raise DatasetError("Output directory already exists; choose a new directory")
    if sample_size < 1:
        raise DatasetError("sample_size must be positive")
    source_hash = file_hash(source)
    index = scan(source, repair_kazakh=repair_kazakh)
    partitions = split(index, seed=seed, control_ratio=control_ratio)
    manifest = {}
    selected = {}
    for name, indices in partitions.items():
        sampled = balanced_sample(indices, index.entries, sample_size, seed)
        chosen = set(sampled)
        for i in indices:
            entry = index.entries[i]
            selected[entry["ticket_id"]] = (name, i in chosen)
            manifest[entry["ticket_id"]] = {"split": name, "annotation_sample": i in chosen}
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Do not expose partial output if a second-pass validation or write fails.
    with tempfile.TemporaryDirectory(prefix=".dataset-", dir=destination.parent) as temporary:
        folder = Path(temporary)
        handles = {}
        try:
            for name in partitions:
                for suffix in ("inputs", "metadata", "annotations"):
                    handles[name, suffix] = (folder / f"{name}.{suffix}.jsonl").open("w", encoding="utf-8")
            observed = set()
            for number, raw in records(source):
                try:
                    ticket = adapt(raw, repair_kazakh=repair_kazakh)
                except DatasetError:
                    raise DatasetError(f"Source changed or invalid record at line {number}") from None
                if ticket.ticket_id not in selected or ticket.ticket_id in observed:
                    raise DatasetError("Source membership changed during preparation")
                observed.add(ticket.ticket_id)
                name, annotated = selected[ticket.ticket_id]
                request = ticket.request.model_dump()
                handles[name, "inputs"].write(json.dumps(request, ensure_ascii=False) + "\n")
                metadata = {"ticket_id": ticket.ticket_id, "reporter_hash": ticket.reporter_hash,
                            "lang_hint": ticket.lang, "server_lang": ticket.server_lang,
                            "original_messages": ticket.original_messages,
                            "normalization_replacements": ticket.replacements}
                handles[name, "metadata"].write(json.dumps(metadata, ensure_ascii=False) + "\n")
                if annotated:
                    annotation = {"ticket_id": ticket.ticket_id, "split": name, "request": request,
                                  "original_messages": ticket.original_messages,
                                  "category": None, "priority": None, "lang": None,
                                  "needs_human": None, "annotator": None, "notes": ""}
                    handles[name, "annotations"].write(json.dumps(annotation, ensure_ascii=False) + "\n")
            if len(observed) != len(selected):
                raise DatasetError("Source membership changed during preparation")
        finally:
            for handle in handles.values():
                handle.close()
        if file_hash(source) != source_hash:
            raise DatasetError("Source changed during preparation; no output published")
        summary = {"seed": seed, "requested_control_ratio": control_ratio,
                   "counts": {name: len(indices) for name, indices in partitions.items()},
                   "actual_control_ratio": len(partitions["control"]) / len(index.entries),
                   "sampling": "balanced round-robin by source language hint and kind",
                   "sample_size_per_partition": sample_size,
                   "holdout_policy": "Do not tune prompts, rules or thresholds on control labels.",
                   "source_fingerprint": source_hash,
                   "audit": index.audit}
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "split_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        folder.rename(destination)
    return summary


def compare(left: Path, right: Path, *, repair_kazakh: bool = False) -> dict:
    first = scan(left, repair_kazakh=repair_kazakh)
    second = scan(right, repair_kazakh=repair_kazakh)
    if any(index.audit["totals"].get("invalid_records") for index in (first, second)):
        raise DatasetError("Resolve invalid records before comparing datasets")
    result = {}
    for field in ("ticket_id", "reporter_hash", "duplicate_hash"):
        a = {e[field] for e in first.entries if e[field] is not None}
        b = {e[field] for e in second.entries if e[field] is not None}
        result[field + "_overlap"] = len(a & b)
    result["left_valid_records"] = len(first.entries)
    result["right_valid_records"] = len(second.entries)
    result["limitation"] = "Zero exact overlap does not prove absence of semantic or temporal leakage."
    return result


def label_ids(value, field: str) -> set[str]:
    """Accept explicit category/priority registries, never guess nested taxonomy semantics."""
    if isinstance(value, dict):
        ids = list(value)
    elif isinstance(value, list):
        ids = []
        for item in value:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict):
                if "code" in item and "id" in item and item["code"] != item["id"]:
                    raise DatasetError(f"Conflicting taxonomy {field} code/id")
                ids.append(item.get("code", item.get("id")))
            else:
                ids.append(None)
    else:
        raise DatasetError(f"Unsupported taxonomy {field}; expected registry mapping or list of IDs/code objects")
    if not ids or any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise DatasetError(f"Invalid taxonomy {field} IDs")
    return set(ids)


def validate_annotations(source: Path, taxonomy: Path, expected: Path) -> dict:
    try:
        registry = json.loads(taxonomy.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, ValueError):
        raise DatasetError("Invalid taxonomy JSON") from None
    if not isinstance(registry, dict):
        raise DatasetError("Expected taxonomy object")
    categories = label_ids(registry.get("categories"), "categories")
    priorities = label_ids(registry.get("priorities"), "priorities")
    category_registry = registry["categories"]
    if isinstance(category_registry, dict):
        category_specs = category_registry.items()
    else:
        category_specs = ((item.get("code", item.get("id")), item)
                          for item in category_registry if isinstance(item, dict))
    kinds = {}
    for code, spec in category_specs:
        if isinstance(spec, dict) and "kind" in spec:
            if spec["kind"] not in ("complaint", "report"):
                raise DatasetError("Invalid category kind in taxonomy")
            kinds[code] = spec["kind"]
    always_human = registry.get("needs_human_always", [])
    if not isinstance(always_human, list) or any(
        not isinstance(code, str) or code not in categories for code in always_human
    ):
        raise DatasetError("Invalid needs_human_always taxonomy registry")
    if not priorities <= {"p0", "p1", "p2", "p3"}:
        raise DatasetError("Taxonomy priorities do not match p0..p3 contract")
    templates = {}
    for number, record in records(expected):
        key = identifier(record.get("ticket_id"), "ticket_id", required=True)
        if key in templates:
            raise DatasetError(f"Duplicate expected ticket ID at line {number}")
        templates[key] = record
    if not templates:
        raise DatasetError("Expected annotation sample is empty")
    seen = set()
    languages = Counter()
    for number, record in records(source):
        key = identifier(record.get("ticket_id"), "ticket_id", required=True)
        if key not in templates or key in seen:
            raise DatasetError(f"Unexpected or repeated annotation ID at line {number}")
        template = templates[key]
        if any(record.get(field) != template.get(field) for field in ("split", "request", "original_messages")):
            raise DatasetError(f"Annotation source or split changed at line {number}")
        if not isinstance(record.get("category"), str) or record["category"] not in categories:
            raise DatasetError(f"Missing or unknown category at line {number}")
        category_kind = kinds.get(record["category"])
        if category_kind is not None and record["request"].get("kind") != category_kind:
            raise DatasetError(f"Category does not match request kind at line {number}")
        if not isinstance(record.get("priority"), str) or record["priority"] not in priorities:
            raise DatasetError(f"Missing or unknown priority at line {number}")
        if record.get("lang") not in ("ru", "kk", "uz", "en", "es", "pt", "und"):
            raise DatasetError(f"Missing or invalid language at line {number}")
        if type(record.get("needs_human")) is not bool:
            raise DatasetError(f"Missing or invalid needs_human at line {number}")
        if record["category"] in always_human and record["needs_human"] is not True:
            raise DatasetError(f"Taxonomy requires human review at line {number}")
        if not isinstance(record.get("annotator"), str) or not record["annotator"].strip():
            raise DatasetError(f"Missing annotator at line {number}")
        if not isinstance(record.get("notes"), str):
            raise DatasetError(f"Invalid annotation notes at line {number}")
        seen.add(key)
        languages[record["lang"]] += 1
    if seen != set(templates):
        raise DatasetError("Annotation file does not cover the expected sample")
    return {"validated_records": len(seen), "languages": dict(languages),
            "limitation": "Schema validation is not independent verification of human label quality."}


def main() -> int:
    parser = argparse.ArgumentParser(description="Wave 1 local dataset tools; no external API calls")
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--input", type=Path, required=True)
    prep.add_argument("--output-dir", type=Path, required=True)
    prep.add_argument("--sample-size", type=int, default=100)
    prep.add_argument("--seed", type=int, default=42)
    prep.add_argument("--control-ratio", type=float, default=.2)
    comp = sub.add_parser("compare")
    comp.add_argument("--left", type=Path, required=True)
    comp.add_argument("--right", type=Path, required=True)
    comp.add_argument("--output", type=Path, required=True)
    annotations = sub.add_parser("validate-annotations")
    annotations.add_argument("--input", type=Path, required=True)
    annotations.add_argument("--taxonomy", type=Path, required=True)
    annotations.add_argument("--expected", type=Path, required=True)
    for command in (audit, prep, comp):
        command.add_argument("--repair-kazakh", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "audit":
            if args.input.resolve() == args.output.resolve() or args.output.exists():
                raise DatasetError("Choose a new audit output path distinct from input")
            report = scan(args.input, repair_kazakh=args.repair_kazakh).audit
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Audit: {report['valid_records']} valid records", file=sys.stderr)
        elif args.command == "prepare":
            report = prepare(args.input, args.output_dir, sample_size=args.sample_size,
                             seed=args.seed, control_ratio=args.control_ratio,
                             repair_kazakh=args.repair_kazakh)
            print(f"Prepared partitions: {report['counts']}", file=sys.stderr)
        elif args.command == "compare":
            if args.output.exists() or args.output.resolve() in (args.left.resolve(), args.right.resolve()):
                raise DatasetError("Choose a new comparison output path distinct from inputs")
            report = compare(args.left, args.right, repair_kazakh=args.repair_kazakh)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Comparison written; review overlap counts locally", file=sys.stderr)
        else:
            report = validate_annotations(args.input, args.taxonomy, args.expected)
            print(f"Validated {report['validated_records']} annotations", file=sys.stderr)
    except (OSError, UnicodeError, DatasetError, EOFError) as exc:
        print(f"Dataset operation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
