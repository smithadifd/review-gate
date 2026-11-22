"""Import reruns complete, and every refusal it can foresee comes before the first write."""
from __future__ import annotations

import hashlib
import io
import os

import pytest

from rig import key, ok
from review_gate.grammar import parse_import_args
from review_gate.receipt_import import run
from review_gate.refusal import Refusal
from test_receipt_import import events, store
from test_receipt_writer import document, write

BLOCK = {"block": 1, "harden": 0, "nit": 0}


def run_import(r, store_text, *, base=None):
    """Run the import with the store argument exactly as given; GitHub reports ``base``."""
    routes = r.repo.routes()
    routes[key(["api", f"repos/{r.repo.slug}/compare/main...{r.subject.head}"])] = [
        ok({"merge_base_commit": {"sha": base or r.repo.base_oid}})]
    r.fake.set_routes(routes)
    out, err = io.StringIO(), io.StringIO()
    code = run(["import", "--store", store_text, "--pr", r.repo.url],
               records=r.port, gh=r.fake.transport(), store_check=r.store.check, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def receipts(r):
    return [e for e in events(r) if e.kind == "review.receipt"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def refused_before_writing(r, directory, name):
    before = len(events(r))
    code, out, err = run_import(r, str(directory))
    assert code == 2 and err.startswith(name + ":"), err
    assert len(events(r)) == before, "the import wrote receipts before its refusal"


# A document already recorded for its slot is skipped, whatever its provenance or recorded base.

def test_import_of_a_document_the_writer_recorded_writes_nothing(ledger_rig):
    r = ledger_rig
    data = document(r)
    write(r, data)
    directory = store(r, [data])
    before = len(events(r))
    for _ in range(2):
        code, out, err = run_import(r, str(directory))
        assert code == 0 and "written=0" in out, err
        assert len(events(r)) == before, "an already recorded document was written again"
    assert "imported_from" not in receipts(r)[0].payload


def test_import_of_one_store_named_with_a_trailing_slash_writes_nothing(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r), document(r, lane="claude", served_model="claude-opus-x")])
    code, out, err = run_import(r, str(directory))
    assert code == 0 and "written=2" in out, err
    before = len(events(r))
    code, out, err = run_import(r, str(directory) + "/")
    assert code == 0 and "written=0" in out, err
    assert len(events(r)) == before, "the same store under another spelling wrote events"
    assert {e.payload["imported_from"]["store"] for e in receipts(r)} == {str(directory)}


def test_import_after_the_merge_base_moved_writes_nothing(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r), document(r, lane="claude", served_model="claude-opus-x")])
    code, out, err = run_import(r, str(directory))
    assert code == 0 and "written=2" in out, err
    before = len(events(r))
    code, out, err = run_import(r, str(directory), base="d" * 40)
    assert code == 0 and "written=0" in out, err
    assert len(events(r)) == before, "a moved merge base wrote events"
    assert {e.payload["base"] for e in receipts(r)} == {r.repo.base_oid}


def test_import_rerun_after_a_live_later_round_writes_nothing(ledger_rig):
    """The store holds rounds 1 and 2; round 3 is written live; the store is imported again."""
    r = ledger_rig
    first = document(r)
    second = document(r, round=2, prior_receipt={"sha256": digest(first)})
    directory = store(r, [first, second])
    code, out, err = run_import(r, str(directory))
    assert code == 0 and "written=2" in out, err
    write(r, document(r, round=3, prior_receipt={"sha256": digest(second)}))
    before = len(events(r))
    code, out, err = run_import(r, str(directory))
    assert code == 0 and "written=0" in out, err
    assert len(events(r)) == before, "the unchanged rerun wrote events"
    assert [e.payload["round"] for e in receipts(r)] == [1, 2, 3]


def test_import_readback_sees_a_receipt_that_was_not_recorded(ledger_rig, monkeypatch):
    """One write returns without recording its receipt."""
    import review_gate.receipt_import as importing
    r = ledger_rig
    lost = document(r)
    directory = store(r, [document(r, lane="claude", served_model="claude-opus-x"), lost])
    append = importing.append_receipt

    def losing_one(lg, item, fields, data):
        return None if data == lost else append(lg, item, fields, data)

    monkeypatch.setattr(importing, "append_receipt", losing_one)
    code, out, err = run_import(r, str(directory))
    assert code == 2 and err.startswith("import_partial:"), err
    assert [e.payload["lane"] for e in receipts(r)] == ["claude"]


def test_import_readback_sees_a_receipt_recorded_during_the_import(ledger_rig, monkeypatch):
    """Another receipt lands between the import's checks and its read-back."""
    import review_gate.receipt_import as importing
    r = ledger_rig
    directory = store(r, [document(r)])
    other = document(r, lane="claude", served_model="claude-opus-x")
    append = importing.append_receipt

    def with_another_writer(lg, item, fields, data):
        event = append(lg, item, fields, data)
        recorded = lg.events(item_id=item, kind="review.receipt")
        extra = importing.prepare_receipt(importing.derive_document(other), other, recorded,
                                          base=r.repo.base_oid)
        append(lg, item, extra, other)
        return event

    monkeypatch.setattr(importing, "append_receipt", with_another_writer)
    code, out, err = run_import(r, str(directory))
    assert code == 2 and err.startswith("import_partial:"), err


# The writer's checks, in the import's preflight: refused before the first write.

def test_import_refuses_a_regenerated_slot_document_before_writing_a_new_lane(ledger_rig):
    r = ledger_rig
    original = document(r)
    assert run_import(r, str(store(r, [original])))[0] == 0
    directory = store(r, [original, document(r, result_path="regenerated"),
                          document(r, lane="claude", served_model="claude-opus-x")])
    refused_before_writing(r, directory, "receipt_supersedes_required")


def test_import_refuses_a_prior_that_is_not_the_previous_round_before_writing(ledger_rig):
    r = ledger_rig
    first = document(r)
    directory = store(r, [first, document(r, round=3, prior_receipt={"sha256": digest(first)})])
    refused_before_writing(r, directory, "receipt_prior_mismatch")


def test_import_refuses_a_sibling_naming_an_older_slot_document_before_writing(ledger_rig):
    r = ledger_rig
    original = document(r)
    named = {"sha256": digest(original)}
    directory = store(r, [original, document(r, settled_from=named),
                          document(r, settled_from=named, result_path="older")])
    refused_before_writing(r, directory, "receipt_supersedes_mismatch")


def test_import_refuses_a_carried_entry_whose_prior_lacks_its_root_before_writing(ledger_rig):
    r = ledger_rig
    first = document(r)
    entry = {"file": "f.py", "line": 1, "description": "Missing root", "cell": "block",
             "residual_root": "f.py:1@" + "0" * 16}
    directory = store(r, [first, document(r, round=2, prior_receipt={"sha256": digest(first)},
                                          blocks_carried=[entry], open_bugs=1, cells=BLOCK)])
    refused_before_writing(r, directory, "receipt_carried_root_missing")


# The import's own checks.

@pytest.mark.parametrize("kind", ["hard_link", "fifo"])
def test_import_reads_only_regular_single_link_files(ledger_rig, kind):
    r = ledger_rig
    directory = store(r, [document(r)])
    receipt = directory / "0" / "review-run.json"
    if kind == "hard_link":
        os.link(receipt, directory / "second-name.json")
    else:
        receipt.rename(directory / "moved.json")
        os.mkfifo(receipt)
    refused_before_writing(r, directory, "import_file_unreadable")


def test_import_matches_a_repository_named_in_another_case(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r, pr_repo=r.subject.repo.upper())])
    code, out, err = run_import(r, str(directory))
    assert code == 0 and "written=1" in out, err
    assert [e.payload["repo"] for e in receipts(r)] == [r.subject.repo]


@pytest.mark.parametrize("answer", [{"number": 8}, {"nameWithOwner": "other/widget"}])
def test_import_refuses_a_github_answer_for_another_pull_request(ledger_rig, answer):
    r = ledger_rig
    r.repo.state1 = answer
    refused_before_writing(r, store(r, [document(r)]), "import_base_unresolved")


def test_import_scans_derived_fields_for_credential_shapes_before_writing(ledger_rig):
    """The sample is assembled at run time; a JSON unicode escape keeps it out of the raw bytes."""
    r = ledger_rig
    placeholder = "requested-model-placeholder"
    escaped = document(r, lane="third", requested_model=placeholder).replace(
        placeholder.encode(), b"\\u0067" + b"hp_" + b"a" * 36)
    refused_before_writing(r, store(r, [document(r), escaped]), "receipt_credential_shape")


def test_import_refuses_a_relative_store_before_reading(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r)])
    with pytest.raises(Refusal) as exc:
        parse_import_args(["import", "--store", "receipt-store", "--pr", r.repo.url])
    assert exc.value.name == "import_store_relative"
    refused_before_writing(r, os.path.relpath(directory), "import_store_relative")
    assert r.fake.calls() == []
