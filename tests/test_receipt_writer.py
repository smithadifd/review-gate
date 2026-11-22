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
    changes = {} if verdict == "approved" else {"verdict": verdict, "open_bugs": None}
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
