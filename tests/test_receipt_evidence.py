"""Receipt refusal paths use real ledger records and fixture-created stores."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest

from rig import ITEM
from review_gate.refusal import Refusal
from test_receipt_import import events, invoke, store
from test_receipt_writer import document, refused, write


def test_import_document_keys_write_no_authority_or_items(ledger_rig):
    r = ledger_rig
    data = document(r, round_grant={"chosen": "granted", "round": 9},
                    grant={"chosen": "granted"}, waiver="f.py:1#root",
                    settled_by="reviewer", adjudicator="reviewer",
                    hardening="f.py:1", hardening_lines=["f.py:1"],
                    decision={"chosen": "granted"}, asks=["grant"],
                    findings=[{"file": "f.py", "line": 1, "description": "Still open", "cell": "block"}],
                    open_bugs=1, cells={"block": 1, "harden": 0, "nit": 0})
    directory = store(r, [data, document(r, lane="claude", served_model="claude-opus-x")])
    before = events(r)
    code, out, err = invoke(r, directory)
    assert code == 0, err
    added = events(r)[len(before):]
    assert not any(e.kind == "review.grant-cite" for e in added), "document wrote a grant cite"
    assert not any(e.kind.startswith("decision.") for e in added), "document wrote a decision event"
    assert not any(e.kind.startswith("item.") for e in added), "document wrote an item event"
    assert {e.kind for e in added} == {"doc.attached", "review.receipt"}
    result = r.run()
    assert result.reason == "open_bug_unwaived", result.err


def test_prior_digest_must_resolve_once_on_item(ledger_rig):
    from ledger import open_ledger
    from ledger.receipts import write_receipt
    r = ledger_rig
    data = document(r)
    first = write(r, data)
    fields = dict(first.payload, lane="claude")
    fields.pop("doc_sha256")
    with open_ledger(r.db, create=False) as lg:
        write_receipt(lg, ITEM, fields, data, harness="review-gate")
    refused(r, "receipt_prior_mismatch", document(
        r, round=2, prior_receipt={"sha256": first.payload["doc_sha256"]}))


@pytest.mark.parametrize("column,value", [(2, "e" * 40), (3, "other"), (4, "2")])
def test_index_slot_must_agree_with_document(ledger_rig, column, value):
    r = ledger_rig
    directory = store(r, [document(r)])
    index = directory / "review-run.index"
    columns = index.read_text().strip().split("\t")
    columns[column] = value
    index.write_text("\t".join(columns) + "\n")
    before = events(r)
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_index_identity_mismatch:"), err
    assert events(r) == before


@pytest.mark.parametrize("problem", ["encoding", "columns", "duplicate"])
def test_index_must_have_one_valid_line_per_path(ledger_rig, problem):
    r = ledger_rig
    directory = store(r, [document(r)])
    index = directory / "review-run.index"
    data = index.read_bytes()
    index.write_bytes(b"\xff" if problem == "encoding" else b"invalid\n" if problem == "columns" else data + data)
    before = events(r)
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_index_invalid:"), err
    assert events(r) == before


def test_import_store_provenance_is_scanned_before_writing(ledger_rig):
    """Assemble the sample the ledger scan refuses only at run time."""
    r = ledger_rig
    directory = store(r, [document(r)])
    renamed = directory.with_name("gh" + "p_" + "a" * 36)
    directory.rename(renamed)
    before = events(r)
    code, out, err = invoke(r, renamed)
    assert code == 2 and err.startswith("receipt_credential_shape:"), err
    assert events(r) == before


def test_import_first_write_failure_is_named_and_retry_completes(ledger_rig, monkeypatch):
    import review_gate.receipt_import as importing
    r = ledger_rig
    directory = store(r, [document(r)])
    before = events(r)
    def interrupted(*args):
        raise OSError("write interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(importing, "append_receipt", interrupted)
        code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_failed: OSError"), err
    assert events(r) == before
    code, out, err = invoke(r, directory)
    assert code == 0 and "written=1" in out, err


def test_concurrent_new_heads_cannot_restart_a_lane(ledger_rig, monkeypatch):
    import review_gate.receipt_writer as writing
    r = ledger_rig
    r.port.database_paths()
    rendezvous = Barrier(2)
    prepare = writing.prepare_receipt
    def synchronized(*args, **kwargs):
        fields = prepare(*args, **kwargs)
        try:
            rendezvous.wait(timeout=0.5)
        except BrokenBarrierError:
            pass
        return fields
    monkeypatch.setattr(writing, "prepare_receipt", synchronized)
    def attempt(head):
        try:
            return write(r, document(r, reviewed_head=head)).kind
        except Refusal as exc:
            return exc.name
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, [r.subject.head, "e" * 40]))
    assert sorted(results) == ["receipt_round_restart", "review.receipt"], "concurrent writers restarted one lane"
    assert len([e for e in events(r) if e.kind == "review.receipt"]) == 1


def test_import_refuses_cyclic_parsed_dependencies(ledger_rig, monkeypatch):
    """Inject a cycle at the parsed-dependency boundary, before ledger writes."""
    from dataclasses import replace
    import hashlib
    import review_gate.receipt_import as importing
    r = ledger_rig
    documents = [document(r), document(r, lane="claude", served_model="claude-opus-x")]
    digests = [hashlib.sha256(data).hexdigest() for data in documents]
    derive = importing.derive_document
    def cyclic(data):
        parsed = derive(data)
        return replace(parsed, source=digests[1 - documents.index(data)])
    monkeypatch.setattr(importing, "derive_document", cyclic)
    directory = store(r, documents)
    before = events(r)
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_dependency_invalid:"), err
    assert events(r) == before, "dependency refusal wrote events"
