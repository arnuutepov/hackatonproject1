import copy
import gzip
import json
import pytest

from support_copilot.dataset import (
    DatasetError, adapt, compare, prepare, records, scan, split, validate_annotations,
)
from support_copilot.normalization import normalize_text
from support_copilot.schemas import TriageRequest


def ticket(number, *, reporter=None, text=None, lang="ru"):
    return {
        "ticket_id": f"synthetic-{number}", "kind": "complaint", "lang": lang,
        "server_lang": lang, "reporter_hash": reporter or f"reporter-{number}",
        "images_count": 2, "category": None, "priority": None,
        "verdict": "PRIVATE_REFERENCE", "punishment": "PRIVATE_REFERENCE",
        "violation": "PRIVATE_REFERENCE",
        "messages": [
            {"role": "player", "text": text or f"Вымышленное обращение номер {number}", "ts": "2026-01-01"},
            {"role": "admin", "text": "PRIVATE_REFERENCE"},
            {"role": "player", "text": "POST_DECISION_REPLY"},
        ],
    }


def write_records(path, values):
    value = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in values)
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(value)
    else:
        path.write_text(value, encoding="utf-8")
    return path


def fixture_records():
    return [ticket(i, lang=["ru", "kk", "uz", "en"][i % 4]) for i in range(20)]


def test_adapter_whitelist_and_cutoff():
    raw = ticket(1)
    before = copy.deepcopy(raw)
    result = adapt(raw)
    assert raw == before
    assert result.request.model_dump() == {
        "kind": "complaint", "messages": [
            {"role": "player", "text": "Вымышленное обращение номер 1", "attachments": 2},
        ],
    }
    assert result.removed_admin and result.dropped_after_admin == 1
    assert "PRIVATE_REFERENCE" not in json.dumps(result.request.model_dump())
    assert "POST_DECISION_REPLY" not in json.dumps(result.original_messages)


def test_admin_outcomes_cannot_change_observable_input():
    raw = ticket(1)
    other = copy.deepcopy(raw)
    other.update(verdict="different", punishment="different", violation="different")
    other["messages"][1]["text"] = "A completely different verdict"
    assert adapt(raw).request == adapt(other).request
    assert adapt(raw).duplicate_hash == adapt(other).duplicate_hash


def test_attachment_counts_are_not_truncated_by_an_invented_limit():
    result = adapt(ticket(1) | {"images_count": 145})
    assert result.request.messages[0].attachments == 145


@pytest.mark.parametrize("change", [
    {"images_count": -1}, {"images_count": True}, {"messages": []},
    {"ticket_id": None}, {"kind": "unknown"},
    {"messages": [{"role": "system", "text": "SECRET"}]},
    {"messages": [{"role": "admin", "text": "SECRET"}]},
    {"messages": [{"role": "player", "text": " "}]},
])
def test_invalid_records_are_not_silently_fixed(change):
    with pytest.raises(DatasetError) as exc:
        adapt(ticket(1) | change)
    assert "SECRET" not in str(exc.value)


def test_kazakh_repair_is_opt_in_contextual_and_keeps_original():
    original = "ќ ґ ѕ є ј ў ї"
    assert normalize_text(original, lang="kk").text == original
    assert normalize_text(original, lang="ru", repair_kazakh=True).text == original
    result = normalize_text(original, server_lang="kk", repair_kazakh=True)
    assert result.original == original
    assert result.text == "қ ғ ң ә ө ұ ү"
    assert result.replacements == 7
    assert result.mapping_version.endswith("provisional")


def test_adapter_preserves_raw_normalization_pair():
    result = adapt(ticket(1, text="ќайда барамын", lang="kk"), repair_kazakh=True)
    assert result.original_messages[0]["text"] == "ќайда барамын"
    assert result.request.messages[0].text == "қайда барамын"


def test_audit_gzip_labels_and_invalid_records(tmp_path):
    valid = ticket(1) | {"category": "example.category", "priority": "p0"}
    path = write_records(tmp_path / "sample.jsonl.gz", [valid, ticket(2) | {"messages": []}])
    audit = scan(path).audit
    assert audit["valid_records"] == 1
    assert audit["totals"]["invalid_records"] == 1
    assert audit["non_null_reference_fields"]["verdict"] == 2
    assert audit["distributions"]["category"] == {"example.category": 1}
    assert "PRIVATE_REFERENCE" not in json.dumps(audit)


def test_json_error_reports_line_without_ticket_text(tmp_path):
    source = tmp_path / "bad.jsonl"
    source.write_text('{"secret":"PRIVATE_REFERENCE"\n')
    with pytest.raises(DatasetError, match="line 1") as exc:
        list(records(source))
    assert "PRIVATE_REFERENCE" not in str(exc.value)


def test_transitive_groups_never_cross_partitions(tmp_path):
    values = [ticket(1, reporter="same", text="Первый текст"),
              ticket(2, reporter="same", text="Повторённый текст"),
              ticket(3, reporter="third", text="  ПОВТОРЁННЫЙ   текст "), ticket(4)]
    index = scan(write_records(tmp_path / "input.jsonl", values))
    parts = split(index, seed=42, control_ratio=.2)
    assert index.audit["connected_groups"] == 2
    assert any({0, 1, 2} <= set(members) for members in parts.values())
    for field in ("reporter_hash", "duplicate_hash"):
        a = {index.entries[i][field] for i in parts["dev"]}
        b = {index.entries[i][field] for i in parts["control"]}
        assert not a & b


def test_giant_group_does_not_swallow_small_requested_control(tmp_path):
    values = [ticket(i, reporter="large-group" if i < 80 else f"person-{i}") for i in range(100)]
    index = scan(write_records(tmp_path / "in.jsonl", values))
    for seed in (1, 42, 99):
        partitions = split(index, seed=seed, control_ratio=.2)
        assert len(partitions["control"]) == 20
        assert set(range(80)) <= set(partitions["dev"])


def test_prepare_blind_aligned_and_stable_under_reordering(tmp_path):
    values = fixture_records()
    source = write_records(tmp_path / "input.jsonl", values)
    output = tmp_path / "prepared"
    summary = prepare(source, output, sample_size=8, repair_kazakh=True)
    assert sum(summary["counts"].values()) == 20
    all_output = "".join(f.read_text() for f in output.glob("*"))
    assert "PRIVATE_REFERENCE" not in all_output
    assert "POST_DECISION_REPLY" not in all_output
    for name in ("dev", "control"):
        inputs = list(records(output / f"{name}.inputs.jsonl"))
        metadata = list(records(output / f"{name}.metadata.jsonl"))
        assert len(inputs) == len(metadata) == summary["counts"][name]
        for (_, payload), (_, meta) in zip(inputs, metadata):
            TriageRequest.model_validate(payload)
            assert meta["ticket_id"].startswith("synthetic-")
        for _, annotation in records(output / f"{name}.annotations.jsonl"):
            assert annotation["category"] is None
            assert annotation["priority"] is None
            assert annotation["lang"] is None
    reversed_source = write_records(tmp_path / "reversed.jsonl", list(reversed(values)))
    second = tmp_path / "second"
    prepare(reversed_source, second, sample_size=8, repair_kazakh=True)
    assert json.loads((output / "split_manifest.json").read_text()) == json.loads((second / "split_manifest.json").read_text())


def test_existing_output_preserved(tmp_path):
    source = write_records(tmp_path / "in.jsonl", fixture_records())
    output = tmp_path / "already"
    output.mkdir()
    (output / "keep").write_text("original")
    with pytest.raises(DatasetError, match="already exists"):
        prepare(source, output)
    assert (output / "keep").read_text() == "original"


def test_source_change_between_passes_does_not_publish_partial_output(tmp_path, monkeypatch):
    import support_copilot.dataset as dataset

    values = fixture_records()
    source = write_records(tmp_path / "in.jsonl", values)
    output = tmp_path / "out"
    original_records = dataset.records
    calls = 0

    def changing_records(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            changed = copy.deepcopy(values)
            changed[0]["messages"][0]["text"] = "Другая проблема, те же ID"
            write_records(source, changed)
        yield from original_records(path)

    monkeypatch.setattr(dataset, "records", changing_records)
    with pytest.raises(DatasetError, match="Source changed"):
        dataset.prepare(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".dataset-*"))


@pytest.mark.parametrize("values", [
    [ticket(1), ticket(1)],
    [ticket(1), ticket(2) | {"messages": []}],
    [ticket(1, reporter="same"), ticket(2, reporter="same")],
])
def test_unusable_dataset_has_no_partial_output(tmp_path, values):
    source = write_records(tmp_path / "in.jsonl", values)
    output = tmp_path / "out"
    with pytest.raises(DatasetError):
        prepare(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".dataset-*"))


def test_compare_detects_cross_dataset_overlap(tmp_path):
    left = write_records(tmp_path / "train.jsonl", [ticket(1), ticket(2, text="Повтор")])
    right = write_records(tmp_path / "valid.jsonl", [ticket(3, text="повтор"), ticket(4, reporter="reporter-1")])
    report = compare(left, right)
    assert report["ticket_id_overlap"] == 0
    assert report["reporter_hash_overlap"] == 1
    assert report["duplicate_hash_overlap"] == 1


def annotation_fixture(tmp_path):
    expected = {"ticket_id": "synthetic-1", "split": "control", "request": {},
                "original_messages": [], "category": None, "priority": None,
                "lang": None, "needs_human": None, "annotator": None, "notes": ""}
    labeled = expected | {"category": "sample.insult", "priority": "p2", "lang": "ru",
                          "needs_human": True, "annotator": "reviewer-A"}
    taxonomy = tmp_path / "taxonomy.json"
    taxonomy.write_text(json.dumps({"categories": [{"id": "sample.insult"}],
                                    "priorities": {"p0": {}, "p1": {}, "p2": {}, "p3": {}}}))
    return write_records(tmp_path / "expected.jsonl", [expected]), taxonomy, labeled


def test_validate_complete_manual_labels(tmp_path):
    expected, taxonomy, labeled = annotation_fixture(tmp_path)
    source = write_records(tmp_path / "labels.jsonl", [labeled])
    assert validate_annotations(source, taxonomy, expected)["validated_records"] == 1


def test_organizer_style_taxonomy_uses_code_and_rejects_conflicts(tmp_path):
    expected, taxonomy, labeled = annotation_fixture(tmp_path)
    taxonomy.write_text(json.dumps({"categories": [{"code": "sample.insult"}],
                                    "priorities": [{"code": "p2"}]}))
    source = write_records(tmp_path / "labels.jsonl", [labeled])
    assert validate_annotations(source, taxonomy, expected)["validated_records"] == 1
    taxonomy.write_text(json.dumps({"categories": [{"code": "sample.insult", "id": "different"}],
                                    "priorities": [{"code": "p2"}]}))
    with pytest.raises(DatasetError, match="Conflicting"):
        validate_annotations(source, taxonomy, expected)


@pytest.mark.parametrize("change", [
    {"category": None}, {"category": "invented"}, {"priority": "p9"},
    {"lang": None}, {"needs_human": 1}, {"annotator": ""},
    {"split": "dev"}, {"request": {"text": "changed"}},
])
def test_reject_incomplete_labels_or_changed_evaluation_source(tmp_path, change):
    expected, taxonomy, labeled = annotation_fixture(tmp_path)
    source = write_records(tmp_path / "labels.jsonl", [labeled | change])
    with pytest.raises(DatasetError):
        validate_annotations(source, taxonomy, expected)


def test_reject_partial_annotations(tmp_path):
    expected, taxonomy, labeled = annotation_fixture(tmp_path)
    source = write_records(tmp_path / "labels.jsonl", [])
    with pytest.raises(DatasetError, match="cover"):
        validate_annotations(source, taxonomy, expected)
