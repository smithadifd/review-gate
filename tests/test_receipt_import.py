"""Import preserves indexed bytes and writes no authority from document keys."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from rig import ITEM, key, ok
from test_receipt_writer import document
from review_gate.receipt_import import run


def store(r, documents):
    directory = r.db.parent / "receipt-store"
    directory.mkdir(mode=0o700, exist_ok=True)
    lines = []
    for n, data in enumerate(documents):
        doc = json.loads(data)
        folder = directory / str(n)
        folder.mkdir(exist_ok=True)
        (folder / "review-run.json").write_bytes(data)
        lines.append("\t".join([hashlib.sha256(data).hexdigest(), f"{n}/review-run.json",
                               doc["reviewed_head"], doc["lane"], str(doc["round"]), str(doc["pr"]), "-"]))
    (directory / "review-run.index").write_text("\n".join(reversed(lines)) + "\n")
    return directory


def invoke(r, directory, *, resolved=True, heads=None):
    routes = r.repo.routes()
    for head in heads or [r.subject.head]:
        endpoint = f"repos/{r.repo.slug}/compare/main...{head}"
        routes[key(["api", endpoint])] = [ok({"merge_base_commit": {"sha": r.repo.base_oid}} if resolved else {})]
    r.fake.set_routes(routes)
    out, err = io.StringIO(), io.StringIO()
    code = run(["import", "--store", str(directory), "--pr", r.repo.url],
               records=r.port, gh=r.fake.transport(), store_check=r.store.check, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def events(r):
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        return lg.events()


def test_import_keeps_bytes_as_stored_and_retries_without_events(ledger_rig):
    r = ledger_rig
    data = document(r, round_grant={"chosen": "granted"}, waiver="not a record", hardening="not an item")
    directory = store(r, [data, document(r, lane="claude", served_model="claude-opus-x")])
    code, out, err = invoke(r, directory)
    assert code == 0, err
    receipts = [e for e in events(r) if e.kind == "review.receipt"]
    assert receipts[1].payload["doc_sha256"] == hashlib.sha256(data).hexdigest()
    assert all(e.payload["imported_from"]["store"] == str(directory) for e in receipts)
    assert all(e.payload["imported_from"]["sha256"] == e.payload["doc_sha256"] for e in receipts)
    assert {e.kind for e in events(r)} == {"item.created", "item.linked", "doc.attached", "review.receipt"}
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 0 and "written=0" in out, err
    assert len(events(r)) == before, "unchanged import wrote events"
    result = r.run()
    assert result.code == 0, result.err


def test_import_stops_when_a_file_differs_from_its_index_line(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r)])
    (directory / "0" / "review-run.json").write_bytes(document(r, verdict="changes_requested"))
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_index_digest_mismatch:"), err
    assert len(events(r)) == before


def test_import_does_not_follow_receipt_links(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r)])
    original = directory / "0" / "review-run.json"
    original.rename(directory / "data.json")
    original.symlink_to(directory / "data.json")
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_file_unreadable:"), err
    assert len(events(r)) == before


def test_import_takes_only_this_repository_and_number(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r), document(r, pr_repo="other/widget"), document(r, pr=8)])
    code, out, err = invoke(r, directory)
    assert code == 0, err
    receipts = [e for e in events(r) if e.kind == "review.receipt"]
    assert len(receipts) == 1
    assert (receipts[0].payload["repo"], receipts[0].payload["pr"]) == (r.subject.repo, r.subject.number)


def test_import_orders_rounds_and_named_siblings(ledger_rig):
    r = ledger_rig
    first = document(r)
    digest = hashlib.sha256(first).hexdigest()
    sibling = document(r, settled_from={"sha256": digest})
    later = document(r, round=2, prior_receipt={"sha256": hashlib.sha256(sibling).hexdigest()})
    directory = store(r, [first, later, sibling])
    code, out, err = invoke(r, directory)
    assert code == 0, err
    receipts = [e for e in events(r) if e.kind == "review.receipt"]
    assert [e.payload["round"] for e in receipts] == [1, 1, 2]
    assert receipts[1].payload["supersedes"] == receipts[0].id
    assert receipts[2].payload["prior_receipt"] == receipts[1].id


@pytest.mark.parametrize("problem,reason", [
    ("oversize", "receipt_doc_too_large"), ("base", "import_base_unresolved"),
    ("repository", "receipt_repository_missing"), ("counts", "receipt_document_inconsistent"),
    ("credentials", "doc_credential_shape")])
def test_import_checks_every_document_before_writing(ledger_rig, problem, reason):
    """The credential sample is assembled at run time for the ledger scan."""
    r = ledger_rig
    changes = {"lane": "third"}
    if problem == "oversize":
        changes["result_path"] = "x" * 65536
    if problem == "repository":
        changes["pr_repo"] = None
    if problem == "counts":
        changes["open_bugs"] = 1
    if problem == "credentials":
        changes["result_path"] = "gh" + "p_" + "a" * 36
    directory = store(r, [document(r), document(r, lane="claude", served_model="claude-opus-x"), document(r, **changes)])
    before = len(events(r))
    code, out, err = invoke(r, directory, resolved=problem != "base")
    assert code == 2 and err.startswith(reason + ":"), err
    assert len(events(r)) == before, "preflight refusal wrote some receipts"


@pytest.mark.parametrize("verb", ["Import", "imported", "import ", "import-extra"])
def test_import_verb_near_misses_read_nothing(ledger_rig, verb):
    from review_gate.grammar import parse_import_args
    from review_gate.refusal import Refusal
    with pytest.raises(Refusal) as exc:
        parse_import_args([verb, "--store", "/unused", "--pr", ledger_rig.repo.url])
    assert exc.value.name == "command_unknown"
    assert ledger_rig.fake.calls() == []


def test_import_rerun_over_a_shorter_index_keeps_recorded_receipts(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r), document(r, lane="claude", served_model="claude-opus-x")])
    assert invoke(r, directory)[0] == 0
    index = directory / "review-run.index"
    index.write_text(index.read_text().splitlines()[0] + "\n")
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 0 and "written=0" in out, err
    assert len(events(r)) == before


def test_import_admits_store_before_opening_index(ledger_rig):
    from test_store import settings
    r = ledger_rig
    directory = store(r, [document(r)])
    (directory / "review-run.index").write_text("not an index")
    settings(r.db.parent.parent, [directory])
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("receipt_store_lane_writable:"), err
    assert len(events(r)) == before
    assert r.fake.calls() == []


def test_import_index_number_must_agree_with_document(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r)])
    index = directory / "review-run.index"
    columns = index.read_text().strip().split("\t")
    columns[5] = "8"
    index.write_text("\t".join(columns) + "\n")
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_index_identity_mismatch:"), err
    assert len(events(r)) == before


def test_import_skips_design_pass_by_name(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r, pr=None, design_row="design"), document(r)])
    code, out, err = invoke(r, directory)
    assert code == 0 and "import_design_pass_skipped\n" in out, err
    assert len([e for e in events(r) if e.kind == "review.receipt"]) == 1


def test_import_resolves_base_once_per_distinct_head(ledger_rig):
    r = ledger_rig
    first = document(r, reviewed_head="e" * 40)
    later = document(r, round=2, prior_receipt={"sha256": hashlib.sha256(first).hexdigest()})
    directory = store(r, [first, later, document(r, lane="claude", served_model="claude-opus-x")])
    code, out, err = invoke(r, directory, heads=["e" * 40, r.subject.head])
    assert code == 0, err
    compares = [c for c in r.fake.calls() if c[0] == "api" and "/compare/" in c[1]]
    assert len(compares) == 2
    assert {c[1].rsplit("...", 1)[1] for c in compares} == {"e" * 40, r.subject.head}


def test_import_rerun_completes_after_interrupted_write(ledger_rig, monkeypatch):
    import review_gate.receipt_import as importing
    r = ledger_rig
    directory = store(r, [document(r), document(r, lane="claude", served_model="claude-opus-x")])
    append = importing.append_receipt
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("write interrupted")
        return append(*args)

    with monkeypatch.context() as patch:
        patch.setattr(importing, "append_receipt", interrupted)
        code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_partial:"), err
    assert len([e for e in events(r) if e.kind == "review.receipt"]) == 1
    code, out, err = invoke(r, directory)
    assert code == 0 and "written=1" in out, err
    assert len([e for e in events(r) if e.kind == "review.receipt"]) == 2


def test_import_changed_file_after_success_writes_nothing(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r)])
    assert invoke(r, directory)[0] == 0
    (directory / "0" / "review-run.json").write_bytes(document(r, result_path="different"))
    before = len(events(r))
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("import_index_digest_mismatch:"), err
    assert len(events(r)) == before

def test_import_third_oversize_document_leaves_all_three_unwritten(ledger_rig):
    r = ledger_rig
    directory = store(r, [document(r), document(r, lane="second"),
                          document(r, lane="third", result_path="x" * 65536)])
    index = directory / "review-run.index"
    index.write_text("\n".join(reversed(index.read_text().splitlines())) + "\n")
    before = events(r)
    code, out, err = invoke(r, directory)
    assert code == 2 and err.startswith("receipt_doc_too_large:"), err
    assert events(r) == before, "write-as-you-go import recorded receipts before the third-file refusal"
