"""The record verb, through the command line entry, over a temporary ledger database."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from review_gate import cli, receipt_record
from test_receipt_writer import document


class Run:
    def __init__(self, code, out, err, stdin):
        self.code, self.out, self.err, self.stdin = code, out, err, stdin


@pytest.fixture
def record(ledger_rig, monkeypatch, capsys):
    """Run ``review-gate record`` (or another argv) with stdin bytes, against the rig's database."""
    r = ledger_rig
    monkeypatch.setenv("LEDGER_DB", str(r.db))
    monkeypatch.setattr(cli, "LedgerRecords", lambda database: r.port)
    monkeypatch.setattr(cli.StoreCheck, "shipped", classmethod(lambda cls: r.store.check))

    def go(data, argv=("record",)):
        buffer = io.BytesIO(data)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(buffer))
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err, buffer)
    return go


def events(r):
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        return lg.events()


def test_a_live_document_writes_one_receipt_and_its_attachment(ledger_rig, record):
    r = ledger_rig
    data = document(r)
    before = len(events(r))
    done = record(data)
    assert done.code == 0, done.err
    assert done.err == ""
    assert done.out == f"written {r.repo.url} round=1 lane=sol\n"
    added = events(r)[before:]
    assert [e.kind for e in added] == ["doc.attached", "review.receipt"]
    receipt = added[1]
    assert receipt.payload["doc_sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt.harness == "review-gate"
    assert "imported_from" not in receipt.payload


def test_the_same_document_again_is_already_recorded(ledger_rig, record):
    r = ledger_rig
    data = document(r)
    assert record(data).code == 0
    before = len(events(r))
    done = record(data)
    assert done.code == 0, done.err
    assert done.out == f"already_recorded {r.repo.url} round=1 lane=sol\n"
    assert len(events(r)) == before


def receipts(r):
    return [e for e in events(r) if e.kind == "review.receipt"]


def test_bytes_first_imported_are_already_recorded_without_calling_the_writer(ledger_rig, record):
    from test_receipt_import import invoke, store
    r = ledger_rig
    data = document(r)
    code, _, err = invoke(r, store(r, [data]))
    assert code == 0, err
    assert "imported_from" in receipts(r)[0].payload
    before = len(events(r))
    done = record(data)
    assert done.code == 0, done.err
    assert done.err == ""
    assert done.out == f"already_recorded {r.repo.url} round=1 lane=sol\n"
    assert len(events(r)) == before


def test_a_concurrent_record_of_the_same_bytes_is_reported_already_recorded(ledger_rig, record, monkeypatch):
    from review_gate.receipt_writer import write_receipt
    r = ledger_rig
    data = document(r)
    read = receipt_record._known

    def read_then_another_writer(*args):
        found = read(*args)
        write_receipt(data, records=r.port, store_check=r.store.check)
        return found
    monkeypatch.setattr(receipt_record, "_known", read_then_another_writer)
    before, receipts_before = len(events(r)), len(receipts(r))
    done = record(data)
    assert done.code == 0, done.err
    assert done.out == f"already_recorded {r.repo.url} round=1 lane=sol\n"
    assert len(receipts(r)) == receipts_before + 1
    assert len(events(r)) == before + 2, "one receipt and its attachment, written once"


def test_closed_standard_input_refuses_as_empty(ledger_rig, record, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", None)
    before = len(events(ledger_rig))
    code = cli.main(["record"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("record_empty: "), captured.err
    assert len(events(ledger_rig)) == before


def test_a_second_lane_is_its_own_receipt(ledger_rig, record):
    r = ledger_rig
    assert record(document(r)).code == 0
    done = record(document(r, lane="claude", served_model="claude-opus-x", requested_model="claude-opus-x"))
    assert done.code == 0, done.err
    assert done.out.startswith("written ") and "lane=claude" in done.out
    assert [e.payload["lane"] for e in events(r) if e.kind == "review.receipt"] == ["sol", "claude"]


@pytest.mark.parametrize("data,name", [
    (b"{not json", "receipt_doc_not_json"),
    (b"[]", "receipt_doc_not_json"),
    (b'{"a": 1, "a": 2}', "receipt_doc_not_json"),
    (b"", "record_empty"),
    (b" " * 65537, "receipt_doc_too_large"),
    (b"x" * 200000, "receipt_doc_too_large"),
])
def test_unusable_input_refuses_by_name_and_writes_nothing(ledger_rig, record, data, name):
    before = len(events(ledger_rig))
    done = record(data)
    assert done.code == 2
    assert done.out == ""
    assert done.err.startswith(name + ": ") or done.err == name + "\n", done.err
    assert len(events(ledger_rig)) == before


def test_oversize_input_never_reaches_the_writer_and_is_not_read_whole(ledger_rig, record, monkeypatch):
    called = []
    monkeypatch.setattr(receipt_record, "write_receipt", lambda *a, **k: called.append(a))
    done = record(b" " * 300000)
    assert done.code == 2
    assert done.err.startswith("receipt_doc_too_large: ")
    assert called == []
    assert done.stdin.tell() <= 65537 + 8192, "stdin was read past the cap by more than one buffer"


def test_a_document_of_exactly_the_cap_is_not_refused_for_size(ledger_rig, record):
    done = record(document(ledger_rig) + b" " * (65536 - len(document(ledger_rig))))
    assert done.code == 0, done.err


def test_a_missing_ledger_db_refuses_by_name(ledger_rig, record, monkeypatch):
    monkeypatch.delenv("LEDGER_DB")
    monkeypatch.setattr(cli, "LedgerRecords", lambda database: pytest.fail("no database was named"))
    before = len(events(ledger_rig))
    done = record(document(ledger_rig))
    assert done.code == 2
    assert done.out == ""
    assert done.err.startswith("records_unavailable: ")
    assert len(events(ledger_rig)) == before


@pytest.mark.parametrize("changes,name", [
    ({"family": "anthropic"}, "receipt_family_mismatch"),
    ({"open_bugs": 1}, "receipt_document_inconsistent"),
    ({"counts_toward_cap": None}, "receipt_document_shape"),
    ({"pr": 999}, "pr_item_missing"),
])
def test_a_refusal_the_writer_makes_stays_refused_and_writes_nothing(ledger_rig, record, changes, name):
    before = len(events(ledger_rig))
    done = record(document(ledger_rig, **changes))
    assert done.code == 2
    assert done.out == ""
    assert done.err.startswith(name + ": "), done.err
    assert len(events(ledger_rig)) == before


def test_a_second_round_one_for_the_same_lane_stays_refused(ledger_rig, record):
    r = ledger_rig
    assert record(document(r)).code == 0
    before = len(events(r))
    done = record(document(r, served_model="gpt-y", requested_model="gpt-y"))
    assert done.code == 2
    assert done.err.startswith("receipt_supersedes_required: ")
    assert len(events(r)) == before


def test_an_error_that_is_not_a_refusal_prints_its_type(ledger_rig, record, monkeypatch):
    def broken(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(receipt_record, "write_receipt", broken)
    done = record(document(ledger_rig))
    assert done.code == 2
    assert done.err == "record_failed: KeyError\n"
    assert done.out == ""


@pytest.mark.parametrize("argv,name", [
    (["records"], "command_unknown"),
    (["Record"], "command_unknown"),
    (["record", "x"], "argument_unknown"),
    (["record", "--flag"], "argument_unknown"),
    (["record", "--"], "argument_unknown"),
    (["record", ""], "argument_unknown"),
])
def test_record_near_misses_read_nothing_and_write_nothing(ledger_rig, record, argv, name):
    before = len(events(ledger_rig))
    done = record(document(ledger_rig), argv=argv)
    assert done.code == 2
    assert done.out == ""
    assert done.err.startswith(name), done.err
    assert done.stdin.tell() == 0, "a near miss read standard input"
    assert len(events(ledger_rig)) == before


def test_the_parser_takes_exactly_the_bare_verb():
    from review_gate.grammar import parse_record_args
    from review_gate.refusal import Refusal
    assert parse_record_args(["record"]) is None
    for argv in ([], ["records"], ["record", "x"], ["record", "record"]):
        with pytest.raises(Refusal):
            parse_record_args(argv)
