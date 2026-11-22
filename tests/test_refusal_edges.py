"""Fail closed on damaged records and impossible dependency answers."""
from dataclasses import replace
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ledger_rig import ledger_rig, plugin
from rig import ITEM
from test_ledger_records import corrupt_event, refused
from review_gate.ledger_records import LedgerRecords
from review_gate.ledger_runtime import load_ledger
from review_gate.records import merge_key


def test_missing_plugin_refuses(plugin, monkeypatch):
    monkeypatch.setattr(sys, "path", [])
    refused("ledger_plugin_missing", load_ledger)


@pytest.mark.parametrize("damage", ["manifest", "package"])
def test_invalid_plugin_refuses(plugin, tmp_path, monkeypatch, damage):
    root = tmp_path / "invalid-plugin"
    metadata = root / ".claude-plugin"
    metadata.mkdir(parents=True)
    manifest = metadata / "plugin.json"
    if damage == "manifest":
        manifest.write_text("{")
    else:
        manifest.write_text('{"name":"ledger-hooks","version":"0.6.1"}')
        (root / "ledger").symlink_to(plugin / "ledger", target_is_directory=True)
    monkeypatch.setattr(sys, "path", [str(root)])
    refused("ledger_plugin_invalid", load_ledger)


def test_preimported_checkout_ledger_refuses(ledger_rig, tmp_path, monkeypatch):
    checkout = tmp_path / "checkout" / "ledger"
    checkout.mkdir(parents=True)
    source = checkout / "__init__.py"
    source.write_text('__version__ = "0.6.1"\n')
    foreign = ModuleType("ledger")
    foreign.__file__ = str(source)
    foreign.__version__ = "0.6.1"
    monkeypatch.setitem(sys.modules, "ledger", foreign)
    refused("ledger_origin_invalid", load_ledger)


def test_invalid_database_symlink_refuses(ledger_rig, tmp_path):
    link = tmp_path / "linked.db"
    link.symlink_to(ledger_rig.db)
    port = LedgerRecords(str(link))
    refused("ledger_db_invalid", port.database_paths)
    assert port.paths is None


def test_relative_database_never_reaches_ledger(ledger_rig, monkeypatch):
    import review_gate.ledger_records as records
    def unexpected_load():
        pytest.fail("relative database reached ledger loading or path resolution")
    monkeypatch.setattr(records, "load_ledger", unexpected_load)
    refused("ledger_db_relative", LedgerRecords("relative.db").database_paths)


def test_malformed_receipt_refuses(ledger_rig):
    r = ledger_rig
    first, _ = r.pair()
    corrupt_event(r, first.id, payload={**first.payload, "unexpected": True})
    refused("receipt_shape_invalid", r.port.receipts, r.subject, ITEM)


@pytest.mark.parametrize("damage", ["orphan", "wrong-prior"])
def test_invalid_supersedes_refuses(ledger_rig, damage):
    r = ledger_rig
    first, _ = r.pair()
    if damage == "orphan":
        event = first
    else:
        event = r.receipt(supersedes=first.id)
    corrupt_event(r, event.id, payload={**event.payload, "supersedes": 999999})
    refused("receipt_supersedes_invalid", r.port.receipts, r.subject, ITEM)


def test_invalid_merge_key_refuses(ledger_rig):
    r = ledger_rig
    key = merge_key(r.subject.repo, r.subject.number, r.subject.head)
    # Still splittable, but not a full sha; removing validation would read absent.
    refused("merge_key_invalid", r.port.merge_state, ITEM, key[:-1])


@pytest.mark.parametrize("damage", ["payload", "transition"])
def test_invalid_merge_record_refuses(ledger_rig, damage):
    from ledger import open_ledger
    r = ledger_rig
    key = merge_key(r.subject.repo, r.subject.number, r.subject.head)
    r.port.write_intent(ITEM, key)
    with open_ledger(r.db, create=False) as lg:
        event = lg.events(item_id=ITEM, kind="review.merge-intent")[-1]
    if damage == "payload":
        corrupt_event(r, event.id, payload={**event.payload, "extra": True})
    else:
        corrupt_event(r, event.id, kind="review.merge-done")
    refused("merge_record_invalid", r.port.merge_state, ITEM, key)


def test_impossible_merge_finish_answer_refuses(ledger_rig, monkeypatch):
    import ledger.receipts as receipts
    r = ledger_rig
    key = merge_key(r.subject.repo, r.subject.number, r.subject.head)
    r.port.write_intent(ITEM, key)
    monkeypatch.setattr(receipts, "write_merge_done", lambda *a, **k: SimpleNamespace(state="started"))
    refused("merge_finish_unrecorded", r.port.write_finished, ITEM, key, r.repo.head)
    assert r.port.merge_state(ITEM, key) == "started"


@pytest.mark.parametrize("stage", ["item", "close"])
def test_missing_close_item_refuses(ledger_rig, stage):
    r = ledger_rig
    if stage == "item":
        refused("close_item_missing", r.port.item, replace(r.subject, items=(ITEM, "it-missing")))
    else:
        refused("close_item_missing", r.port.close_items, ("it-missing",), r.subject.url, r.subject.head)


def test_unknown_recovery_state_refuses(ledger_rig, monkeypatch):
    r = ledger_rig
    monkeypatch.setattr(r.port, "merge_state", lambda *a: "unknown")
    closed = []
    monkeypatch.setattr(r.port, "close_items", lambda *a: closed.append(a))
    result = r.run(recovery=True)
    assert result.code == 2
    assert result.reason == "records_answer_invalid", result.err
    assert closed == []
    assert r.merge_calls() == []


def test_recovery_output_failure_refuses(ledger_rig):
    import io
    from review_gate.recovery import run
    class BrokenOutput:
        def write(self, text):
            raise OSError("output unavailable")
    r = ledger_rig
    key = merge_key(r.subject.repo, r.subject.number, r.subject.head)
    r.port.write_intent(ITEM, key)
    r.port.write_finished(ITEM, key, r.repo.head)
    r.fake.set_routes(r.repo.routes())
    argv = r.repo.argv()
    argv[0] = "recover"
    err = io.StringIO()
    code = run(argv, records=r.port, gh=r.fake.transport(), store_check=r.store.check,
               out=BrokenOutput(), err=err)
    assert code == 2
    assert err.getvalue() == "recovery_failed: OSError\n"
    assert r.merge_calls() == []
