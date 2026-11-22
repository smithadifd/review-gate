"""Receipt documents, written through the real ledger, agree with the reader."""
from __future__ import annotations

import hashlib
import json

import pytest

from rig import ITEM
from review_gate.refusal import Refusal


def document(r, **changes):
    value = {"pr_repo": r.subject.repo, "pr": r.subject.number,
             "reviewed_head": r.subject.head, "base": r.repo.base_oid,
             "lane": "sol", "round": 1, "counts_toward_cap": True,
             "served_model": "gpt-x", "requested_model": "gpt-x",
             "tier": "full-live", "changed_files_sha256": r.subject.files_sha256,
             "severity_rubric": "v2", "verdict": "approved", "open_bugs": 0,
             "cells": {"block": 0, "harden": 0, "nit": 0}, "findings": [],
             "hardened_carried": [], "blocks_carried": [], "nits_carried": [],
             "resolved_carried": [], "standing_carried": [], "unsettled_carried": []}
    value.update(changes)
    return json.dumps(value).encode()


def write(r, data):
    from review_gate.receipt_writer import write_receipt
    return write_receipt(data, records=r.port, store_check=r.store.check)


def refused(r, name, data):
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        before = len(lg.events())
    with pytest.raises(Refusal) as exc:
        write(r, data)
    assert exc.value.name == name, exc.value.line()
    with open_ledger_readonly(r.db) as lg:
        assert len(lg.events()) == before, "refused document wrote events"


@pytest.mark.parametrize("source_key,verdict", [
    ("settled_from", "approved"), ("adjudicated_from", "unparseable")])
def test_replacing_record_in_a_deciding_slot_refuses(ledger_rig, source_key, verdict):
    r = ledger_rig
    changes = ({"findings": [{"file": "f.py", "line": 1, "description": "Open finding", "cell": "block"}],
                "open_bugs": 1, "cells": {"block": 1, "harden": 0, "nit": 0}}
               if verdict == "approved" else {"verdict": verdict, "open_bugs": None})
    first = write(r, document(r, **changes))
    write(r, document(r, lane="claude", served_model="claude-opus-x"))
    write(r, document(r, **{source_key: {"sha256": first.payload["doc_sha256"]}}))
    with pytest.raises(Refusal) as exc:
        r.port.receipts(r.subject, ITEM)
    assert exc.value.name == "receipt_settlement_unsupported", exc.value.line()


def test_event_fields_come_from_the_document(ledger_rig):
    r = ledger_rig
    data = document(r, imported_from={"store": "/unused", "sha256": "0" * 64})
    first = write(r, data)
    assert first.payload["doc_sha256"] == hashlib.sha256(data).hexdigest()
    assert first.payload["family"] == "openai"
    assert first.harness == "review-gate"
    assert "imported_from" not in first.payload
    assert write(r, data).id == first.id
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        assert [e.kind for e in lg.events()][2:] == ["doc.attached", "review.receipt"]


@pytest.mark.parametrize("changes,name", [
    ({"family": "anthropic"}, "receipt_family_mismatch"),
    ({"counts_toward_cap": None}, "receipt_document_shape"),
    ({"round": True}, "receipt_document_shape"),
    ({"pr": 7.0}, "receipt_document_shape"),
    ({"open_bugs": 1}, "receipt_document_inconsistent"),
    ({"cells": {"block": 1, "harden": 0, "nit": 0}}, "receipt_document_inconsistent"),
    ({"findings": [{"file": "f.py", "line": 1, "description": "Count differs", "cell": "other"}]},
     "receipt_cell_unknown"),
])
def test_document_disagreements_refuse_without_writes(ledger_rig, changes, name):
    refused(ledger_rig, name, document(ledger_rig, **changes))


def test_repeated_keys_refuse_without_writes(ledger_rig):
    data = document(ledger_rig)
    refused(ledger_rig, "receipt_doc_not_json", data[:-1] + b', "verdict": "approved"}')


def test_missing_cap_flag_refuses_without_writes(ledger_rig):
    value = json.loads(document(ledger_rig))
    del value["counts_toward_cap"]
    refused(ledger_rig, "receipt_document_shape", json.dumps(value).encode())


def test_round_numbers_continue_across_heads(ledger_rig):
    r = ledger_rig
    write(r, document(r))
    refused(r, "receipt_round_restart", document(r, reviewed_head="f" * 40))


def test_second_document_for_a_slot_needs_a_named_source(ledger_rig):
    r = ledger_rig
    write(r, document(r))
    refused(r, "receipt_supersedes_required", document(r, result_sha256="1" * 64))


def test_finding_locations_must_be_waivable(ledger_rig):
    entry = {"file": "a:b.py", "line": 1, "description": "Location", "cell": "block"}
    refused(ledger_rig, "receipt_finding_id_invalid", document(
        ledger_rig, findings=[entry], open_bugs=1, cells={"block": 1, "harden": 0, "nit": 0}))


def test_carried_entries_are_counted_and_keep_their_root(ledger_rig):
    r = ledger_rig
    entry = {"file": "f.py", "line": 1, "description": "Count this", "cell": "block"}
    first = write(r, document(r, findings=[entry], open_bugs=1,
                              cells={"block": 1, "harden": 0, "nit": 0}))
    root = "f.py:1@" + hashlib.sha256(b"Count this").hexdigest()[:16]
    carried = dict(entry, line=9, residual_root=root)
    second = write(r, document(r, round=2, prior_receipt={"sha256": first.payload["doc_sha256"]},
                               blocks_carried=[carried], open_bugs=1,
                               cells={"block": 1, "harden": 0, "nit": 0}))
    finding = second.payload["findings"][0]
    assert finding["id"].split("#")[1] == first.payload["findings"][0]["id"].split("#")[1]
    assert finding["carried_from"] == first.id


def test_carried_target_must_hold_the_root(ledger_rig):
    r = ledger_rig
    first = write(r, document(r))
    entry = {"file": "f.py", "line": 1, "description": "Missing root", "cell": "block",
             "residual_root": "f.py:1@" + "0" * 16}
    refused(r, "receipt_carried_root_missing", document(
        r, round=2, prior_receipt={"sha256": first.payload["doc_sha256"]},
        blocks_carried=[entry], open_bugs=1, cells={"block": 1, "harden": 0, "nit": 0}))


def test_unlocated_findings_have_description_bound_roots(ledger_rig):
    r = ledger_rig
    entries = [{"file": None, "line": None, "description": text, "cell": "block"}
               for text in ("First count", "Second count")]
    event = write(r, document(r, findings=entries, open_bugs=2,
                             cells={"block": 2, "harden": 0, "nit": 0}))
    ids = [f["id"] for f in event.payload["findings"]]
    assert ids[0] != ids[1], "unlocated descriptions must have different roots"
    assert all(value.startswith("/unlocated:1#") for value in ids)


def test_writer_and_reader_share_waivers_along_a_carry(ledger_rig):
    r = ledger_rig
    entry = {"file": "f.py", "line": 1, "description": "Count this", "cell": "block"}
    first = write(r, document(r, findings=[entry], open_bugs=1,
                              cells={"block": 1, "harden": 0, "nit": 0}))
    write(r, document(r, lane="claude", served_model="claude-opus-x"))
    result = r.run()
    assert result.reason == "open_bug_unwaived", result.err
    ident = first.payload["findings"][0]["id"]
    assert ident in result.err
    from review_gate.records import waiver_grant_id
    r.grant(waiver_grant_id(r.subject.repo, r.subject.number, ident))
    root = "f.py:1@" + hashlib.sha256(b"Count this").hexdigest()[:16]
    write(r, document(r, round=2, prior_receipt={"sha256": first.payload["doc_sha256"]},
                      blocks_carried=[dict(entry, line=5, residual_root=root)], open_bugs=1,
                      cells={"block": 1, "harden": 0, "nit": 0}))
    result = r.run()
    assert result.code == 0, result.err


@pytest.mark.parametrize("older_head", [False, True])
def test_older_siblings_leave_round_cap_and_carry_usable(ledger_rig, older_head):
    r = ledger_rig
    head = "e" * 40 if older_head else r.subject.head
    entry = {"file": "f.py", "line": 1, "description": "Still open", "cell": "block"}
    changes = dict(reviewed_head=head, findings=[entry], open_bugs=1,
                   cells={"block": 1, "harden": 0, "nit": 0})
    first = write(r, document(r, **changes))
    sibling = write(r, document(r, **changes, settled_from={"sha256": first.payload["doc_sha256"]}))
    root = "f.py:1@" + hashlib.sha256(b"Still open").hexdigest()[:16]
    write(r, document(r, round=2, prior_receipt={"sha256": sibling.payload["doc_sha256"]},
                      blocks_carried=[dict(entry, line=8, residual_root=root)], open_bugs=1,
                      cells={"block": 1, "harden": 0, "nit": 0}))
    write(r, document(r, lane="claude", served_model="claude-opus-x"))
    r.port.round_cap(r.subject, ITEM)
    from review_gate.records import waiver_grant_id
    r.grant(waiver_grant_id(r.subject.repo, r.subject.number, first.payload["findings"][0]["id"]))
    result = r.run()
    assert result.code == 0, result.err


def test_writer_admits_database_before_writing(ledger_rig):
    from test_store import settings
    r = ledger_rig
    settings(r.db.parent.parent, [r.db.parent])
    refused(r, "receipt_store_lane_writable", document(r))


def test_missing_database_stays_missing(ledger_rig):
    from review_gate.ledger_records import LedgerRecords
    r = ledger_rig
    absent = r.db.parent / "absent.db"
    r.port = LedgerRecords(str(absent), plugin_check=r.plugin_check)
    with pytest.raises(Refusal) as exc:
        write(r, document(r))
    assert exc.value.name == "receipt_store_database_missing"
    assert not absent.exists()


def test_database_removed_after_admission_is_not_created(ledger_rig, monkeypatch):
    r = ledger_rig
    check = r.store.check.check
    saved = r.db.with_suffix(".saved")

    def move_after_check(paths):
        result = check(paths)
        r.db.rename(saved)
        return result

    monkeypatch.setattr(r.store.check, "check", move_after_check)
    with pytest.raises(Refusal) as exc:
        write(r, document(r))
    assert exc.value.name == "receipt_store_database_missing"
    assert not r.db.exists(), "write open created a missing database"
    saved.rename(r.db)


def test_working_directory_ledger_package_is_not_loaded(ledger_rig, tmp_path, monkeypatch):
    r = ledger_rig
    local = tmp_path / "local"
    package = local / "ledger"
    package.mkdir(parents=True)
    marker = local / "loaded"
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\nraise RuntimeError('local ledger')\n")
    monkeypatch.chdir(local)
    event = write(r, document(r))
    assert event.kind == "review.receipt"
    assert not marker.exists(), "working-directory ledger was loaded"
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", "import review_gate.receipt_writer"],
        cwd=local, env=dict(os.environ, PYTHONPATH=str(root)),
        capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert not marker.exists(), "fresh import loaded the working-directory ledger"


def test_retry_with_different_attribution_refuses_by_name(ledger_rig):
    from ledger import open_ledger
    from ledger.receipts import write_receipt
    from review_gate.receipt_document import derive_document
    from review_gate.receipt_writer import prepare_receipt
    r = ledger_rig
    data = document(r)
    fields = prepare_receipt(derive_document(data), data, [], base=r.repo.base_oid)
    with open_ledger(r.db, create=False) as lg:
        write_receipt(lg, ITEM, fields, data, harness="other-runner")
    refused(r, "receipt_idempotency_conflict", data)


def test_unknown_model_is_recorded_as_unknown(ledger_rig):
    event = write(ledger_rig, document(ledger_rig, served_model="unlisted-model"))
    assert event.payload["family"] == "unknown"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_writer_requires_one_existing_item(ledger_rig, ambiguous):
    r = ledger_rig
    if ambiguous:
        r.append("item.created", {"type": "ticket", "title": "Other pull request"}, item="it-other")
        r.append("item.linked", {"rel": "pr", "target": r.repo.url}, item="it-other")
        data, reason = document(r), "pr_item_ambiguous"
    else:
        data, reason = document(r, pr=8), "pr_item_missing"
    refused(r, reason, data)


def test_duplicate_finding_ids_refuse(ledger_rig):
    entry = {"file": "f.py", "line": 1, "description": "Same finding", "cell": "block"}
    refused(ledger_rig, "receipt_finding_duplicate", document(
        ledger_rig, findings=[entry, entry], open_bugs=2, cells={"block": 2, "harden": 0, "nit": 0}))


@pytest.mark.parametrize("kind", ["missing", "other_lane", "wrong_round"])
def test_prior_digest_must_name_previous_round_of_lane(ledger_rig, kind):
    r = ledger_rig
    first = write(r, document(r, lane="claude" if kind == "other_lane" else "sol"))
    digest = "0" * 64 if kind == "missing" else first.payload["doc_sha256"]
    refused(r, "receipt_prior_mismatch", document(
        r, round=3 if kind == "wrong_round" else 2, prior_receipt={"sha256": digest}))


@pytest.mark.parametrize("kind", ["missing", "older", "other_slot"])
def test_sibling_source_must_be_newest_document_of_slot(ledger_rig, kind):
    r = ledger_rig
    first = write(r, document(r))
    if kind == "older":
        write(r, document(r, settled_from={"sha256": first.payload["doc_sha256"]}))
    if kind == "other_slot":
        first = write(r, document(r, lane="claude", served_model="claude-opus-x"))
    digest = "0" * 64 if kind == "missing" else first.payload["doc_sha256"]
    refused(r, "receipt_supersedes_mismatch", document(
        r, settled_from={"sha256": digest}, result_path="another"))


def test_unlocated_findings_across_receipts_have_distinct_roots(ledger_rig):
    r = ledger_rig
    ids = []
    for lane, text in [("sol", "First description"), ("claude", "Second description")]:
        entry = {"description": text, "cell": "block"}
        event = write(r, document(r, lane=lane, findings=[entry], open_bugs=1,
                                  cells={"block": 1, "harden": 0, "nit": 0}))
        ids.append(event.payload["findings"][0]["id"])
    assert ids[0] != ids[1], "unlocated roots must bind the first-raise description"


def test_concurrent_exact_writers_append_one_receipt(ledger_rig):
    from concurrent.futures import ThreadPoolExecutor
    r = ledger_rig
    r.port.database_paths()
    data = document(r)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: write(r, data), range(2)))
    assert results[0].id == results[1].id
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        assert len(lg.events(kind="review.receipt")) == 1
        assert len(lg.events(kind="doc.attached")) == 1


def test_only_document_bytes_supply_decided_fields():
    import inspect
    from review_gate.receipt_writer import write_receipt
    from review_gate.grammar import parse_import_args
    assert tuple(inspect.signature(write_receipt).parameters) == ("document", "records", "store_check")
    for flag in ("--round", "--lane", "--family", "--verdict", "--supersedes"):
        with pytest.raises(Refusal):
            parse_import_args(["import", "--store", "/unused", "--pr",
                               "https://github.com/example/repo/pull/1", flag, "value"])


@pytest.mark.parametrize("fragment", [
    b'NaN', b'Infinity', b'-Infinity', b'1e999', b'"\\ud800"', b'{"\\udfff": 1}',
    b'[' * 1100 + b'{"verdict": 0, "verdict": 1}' + b']' * 1100,
], ids=["nan", "infinity", "negative-infinity", "overflow", "surrogate-value", "surrogate-key", "deep-repeated-key"])
def test_document_numbers_and_text_have_one_json_meaning(ledger_rig, fragment):
    data = document(ledger_rig)
    refused(ledger_rig, "receipt_doc_not_json", data[:-1] + b', "extra": ' + fragment + b'}')
