"""Documents the review runner publishes, and its merge reader counts, write through the real ledger.

Every fixture is synthesized. Every case runs through write_receipt into a
temporary ledger, because derive_document alone passes a repeated id and an
open_bugs mismatch that the ledger refuses.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from review_gate.refusal import Refusal
from test_receipt_import import events, invoke, store
from test_receipt_writer import document, write


def sha(data):
    return hashlib.sha256(data).hexdigest()


def root(path, line, text):
    """The runner's first-raise root text: ``<path>:<line>@<16 hex of the description>``."""
    return f"{path}:{line}@" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cells(block=0, harden=0, nit=0):
    return {"block": block, "harden": harden, "nit": nit}


def entry(text, cell, path="f.py", line=1, **extra):
    return dict({"file": path, "line": line, "description": text, "cell": cell}, **extra)


def residual(raised, cell, key):
    """An own finding of this round that the runner matched to an earlier finding."""
    return dict(raised, cell=cell, residual_of=key, residual_root=key, match="mapped-line", touched=False)


def first_round(r, findings, **changes):
    counts = cells(**{name: sum(f["cell"] == name for f in findings) for name in ("block", "harden", "nit")})
    return write(r, document(r, findings=findings, cells=counts, open_bugs=counts["block"], **changes))


def second_round(r, first, **changes):
    return document(r, round=2, prior_receipt={"sha256": first.payload["doc_sha256"]}, **changes)


def blocks(event):
    return sum(f["cell"] == "block" for f in event.payload["findings"])


def refuses(r, name, data, detail=None):
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(r.db) as lg:
        before = len(lg.events())
    with pytest.raises(Refusal) as exc:
        write(r, data)
    assert exc.value.name == name, exc.value.line()
    if detail is not None:
        assert exc.value.detail == detail, exc.value.line()
    with open_ledger_readonly(r.db) as lg:
        assert len(lg.events()) == before, "refused document wrote events"


# C1: hardened_carried and nits_carried enter no cell.

@pytest.mark.parametrize("carry", ["harden", "nit", "both"])
def test_carried_hardens_and_nits_enter_no_cell(ledger_rig, carry):
    r = ledger_rig
    harden = entry("Harden this", "harden", line=2)
    nit = entry("Tidy this", "nit", path="g.py", line=4)
    first = first_round(r, [harden, nit])
    changes = {}
    if carry in ("harden", "both"):
        changes["hardened_carried"] = [dict(harden, line=3, residual_root=root("f.py", 2, "Harden this"))]
    if carry in ("nit", "both"):
        changes["nits_carried"] = [dict(nit, residual_root=root("g.py", 4, "Tidy this"), repeats=1)]
    data = second_round(r, first, findings=[entry("New bug", "block", line=9)],
                        cells=cells(block=1), open_bugs=1, **changes)
    event = write(r, data)
    assert event.payload["open_bugs"] == json.loads(data)["open_bugs"] == blocks(event) == 1
    carried = [f["cell"] for f in event.payload["findings"] if f.get("carried_from") == first.id]
    assert carried == {"harden": ["harden"], "nit": ["nit"], "both": ["harden", "nit"]}[carry]


def test_a_located_resolved_carried_harden_leaves_the_event(ledger_rig):
    r = ledger_rig
    harden = entry("Harden this", "harden", line=2)
    first = first_round(r, [harden])
    event = write(r, second_round(
        r, first, hardened_carried=[dict(harden, residual_root=root("f.py", 2, "Harden this"))],
        resolved_carried=[{"file": "f.py", "line": 2, "description": "Harden this"}]))
    assert event.payload["findings"] == []


def test_an_unlocated_carried_harden_is_never_resolved(ledger_rig):
    r = ledger_rig
    harden = entry("Harden this", "harden", line=2)
    first = first_round(r, [harden])
    carried = dict(harden, file=None, line=None, residual_root=root("f.py", 2, "Harden this"))
    event = write(r, second_round(
        r, first, hardened_carried=[carried],
        resolved_carried=[{"file": None, "line": None, "description": "Harden this"}]))
    assert [(f["cell"], f["carried_from"]) for f in event.payload["findings"]] == [("harden", first.id)]


@pytest.mark.parametrize("key,cell", [
    ("hardened_carried", "block"), ("blocks_carried", "nit"), ("nits_carried", "harden")])
def test_a_carried_entry_outside_its_list_cell_refuses(ledger_rig, key, cell):
    r = ledger_rig
    listed = {"hardened_carried": "harden", "blocks_carried": "block", "nits_carried": "nit"}[key]
    raised = entry("Carried", listed, line=2)
    first = first_round(r, [raised])
    carried = dict(raised, cell=cell, residual_root=root("f.py", 2, "Carried"), repeats=1)
    count = cells(block=1) if key == "blocks_carried" else cells()
    refuses(r, "receipt_document_inconsistent",
            second_round(r, first, cells=count, open_bugs=count["block"], **{key: [carried]}),
            f"a {key} entry is not in the {listed} cell")


# C2: a settled sibling of an adjudication.

def chain(r):
    """An unparseable receipt, its adjudication, and the settled sibling of that adjudication."""
    original = document(r, verdict="unparseable", open_bugs=None)
    adjudicated = document(r, adjudicated_from={"sha256": sha(original)})
    settled = document(r, adjudicated_from={"sha256": sha(original)},
                       settled_from={"sha256": sha(adjudicated)})
    return original, adjudicated, settled


def test_a_settled_adjudication_supersedes_the_adjudication(ledger_rig):
    r = ledger_rig
    original, adjudicated, settled = [write(r, data) for data in chain(r)]
    assert adjudicated.payload["supersedes"] == original.id
    assert settled.payload["supersedes"] == adjudicated.id


@pytest.mark.parametrize("keys", [
    ("settled_from", "reparsed_from"), ("adjudicated_from", "reparsed_from"),
    ("settled_from", "adjudicated_from", "reparsed_from")])
def test_other_sibling_source_combinations_refuse(ledger_rig, keys):
    r = ledger_rig
    first = write(r, document(r))
    refuses(r, "receipt_document_shape",
            document(r, **{key: {"sha256": first.payload["doc_sha256"]} for key in keys}), "sibling source")


@pytest.mark.parametrize("named", ["other_event", "the_source", "missing", "source_supersedes_nothing"])
def test_adjudicated_from_must_name_what_the_source_supersedes(ledger_rig, named):
    r = ledger_rig
    if named == "source_supersedes_nothing":
        plain = write(r, document(r)).payload["doc_sha256"]
        data = document(r, settled_from={"sha256": plain}, adjudicated_from={"sha256": plain})
    else:
        original, adjudicated, _ = chain(r)
        write(r, original)
        write(r, adjudicated)
        other = write(r, document(r, lane="claude", served_model="claude-opus-x")).payload["doc_sha256"]
        target = {"other_event": other, "the_source": sha(adjudicated), "missing": "0" * 64}[named]
        data = document(r, settled_from={"sha256": sha(adjudicated)}, adjudicated_from={"sha256": target})
    refuses(r, "receipt_supersedes_mismatch", data)


def test_import_writes_a_settled_adjudication_chain_in_order(ledger_rig):
    r = ledger_rig
    original, adjudicated, settled = chain(r)
    code, out, err = invoke(r, store(r, [settled, original, adjudicated]))
    assert code == 0, err
    receipts = [e for e in events(r) if e.kind == "review.receipt"]
    assert [e.payload["doc_sha256"] for e in receipts] == [sha(original), sha(adjudicated), sha(settled)]
    assert receipts[1].payload["supersedes"] == receipts[0].id
    assert receipts[2].payload["supersedes"] == receipts[1].id


# C3: one id across lists is one event entry.

@pytest.mark.parametrize("own_cell", ["nit", "harden"])
def test_an_id_in_findings_and_blocks_carried_is_one_block_entry(ledger_rig, own_cell):
    """A nit or harden finding that matched a block keeps the block carried (one root, two lists).

    The harden case records the block only: a harden sharing an id with a block leaves the event.
    """
    r = ledger_rig
    bug = entry("Open bug", "block", line=3)
    first = first_round(r, [bug])
    key = root("f.py", 3, "Open bug")
    data = second_round(r, first, findings=[residual(bug, own_cell, key)],
                        blocks_carried=[dict(bug, residual_root=key)],
                        cells=dict(cells(block=1), **{own_cell: 1}), open_bugs=1)
    event = write(r, data)
    ident = first.payload["findings"][0]["id"]
    assert event.payload["findings"] == [{"id": ident, "cell": "block", "carried_from": first.id}]
    assert event.payload["open_bugs"] == json.loads(data)["open_bugs"] == blocks(event) == 1


def test_a_repeated_nit_in_findings_and_nits_carried_is_one_entry(ledger_rig):
    r = ledger_rig
    nit = entry("Tidy this", "nit", line=4)
    first = first_round(r, [nit])
    key = root("f.py", 4, "Tidy this")
    event = write(r, second_round(r, first, findings=[residual(nit, "nit", key)],
                                  nits_carried=[dict(nit, residual_root=key, repeats=2)], cells=cells(nit=1)))
    assert event.payload["findings"] == [
        {"id": first.payload["findings"][0]["id"], "cell": "nit", "carried_from": first.id}]


@pytest.mark.parametrize("matched", [True, False], ids=["residual", "re-raised"])
def test_two_block_entries_on_one_id_refuse(ledger_rig, matched):
    r = ledger_rig
    bug = entry("Open bug", "block", line=3)
    first = first_round(r, [bug])
    key = root("f.py", 3, "Open bug")
    own = residual(bug, "block", key) if matched else dict(bug)
    refuses(r, "receipt_finding_duplicate",
            second_round(r, first, findings=[own], blocks_carried=[dict(bug, residual_root=key)],
                         cells=cells(block=2), open_bugs=2),
            first.payload["findings"][0]["id"])


@pytest.mark.parametrize("key", ["findings", "hardened_carried", "blocks_carried", "nits_carried"])
def test_an_id_twice_in_one_list_refuses(ledger_rig, key):
    r = ledger_rig
    cell = {"findings": "nit", "hardened_carried": "harden", "blocks_carried": "block", "nits_carried": "nit"}[key]
    raised = entry("Twice", cell, line=6)
    first = first_round(r, [raised])
    twice = raised if key == "findings" else dict(raised, residual_root=root("f.py", 6, "Twice"), repeats=1)
    count = {"findings": cells(nit=2), "blocks_carried": cells(block=2)}.get(key, cells())
    refuses(r, "receipt_finding_duplicate",
            second_round(r, first, cells=count, open_bugs=count["block"], **{key: [twice, twice]}))


# C4: carried entries with an absent residual_root.

def unlocated_copy(raised, key, absent):
    copy = dict(raised)
    if absent == "null":
        copy["residual_root"] = None
    if key == "nits_carried":
        copy["repeats"] = 1
    return copy


@pytest.mark.parametrize("absent", ["missing", "null"])
@pytest.mark.parametrize("key", ["nits_carried", "blocks_carried"])
def test_unlocated_carried_entries_without_a_root_link_to_the_prior(ledger_rig, key, absent):
    r = ledger_rig
    cell = "nit" if key == "nits_carried" else "block"
    raised = {"file": None, "line": None, "description": "No locator", "cell": cell}
    first = first_round(r, [raised])
    count = cells(block=1) if cell == "block" else cells()
    event = write(r, second_round(r, first, cells=count, open_bugs=count["block"],
                                  **{key: [unlocated_copy(raised, key, absent)]}))
    assert event.payload["findings"] == [
        {"id": first.payload["findings"][0]["id"], "cell": cell, "carried_from": first.id}]


@pytest.mark.parametrize("absent", ["missing", "null"])
@pytest.mark.parametrize("key", ["nits_carried", "blocks_carried"])
def test_the_same_carried_entries_at_round_one_refuse(ledger_rig, key, absent):
    r = ledger_rig
    cell = "nit" if key == "nits_carried" else "block"
    raised = {"file": None, "line": None, "description": "No locator", "cell": cell}
    count = cells(block=1) if cell == "block" else cells()
    refuses(r, "receipt_document_shape",
            document(r, cells=count, open_bugs=count["block"], **{key: [unlocated_copy(raised, key, absent)]}),
            "carried root")


@pytest.mark.parametrize("absent", ["missing", "null"])
def test_a_located_carried_block_without_a_root_links_through_its_own_id(ledger_rig, absent):
    r = ledger_rig
    bug = entry("Located bug", "block", line=3)
    first = first_round(r, [bug])
    event = write(r, second_round(r, first, blocks_carried=[unlocated_copy(bug, "blocks_carried", absent)],
                                  cells=cells(block=1), open_bugs=1))
    assert event.payload["findings"] == [
        {"id": first.payload["findings"][0]["id"], "cell": "block", "carried_from": first.id}]


@pytest.mark.parametrize("absent", ["missing", "null"])
@pytest.mark.parametrize("key", ["nits_carried", "hardened_carried"])
def test_a_located_carried_nit_or_harden_without_a_root_refuses(ledger_rig, key, absent):
    r = ledger_rig
    raised = entry("Located", "nit" if key == "nits_carried" else "harden", line=3)
    first = first_round(r, [raised])
    refuses(r, "receipt_document_shape",
            second_round(r, first, **{key: [unlocated_copy(raised, key, absent)]}), "carried root")


def test_a_residual_finding_without_a_root_refuses(ledger_rig):
    r = ledger_rig
    nit = entry("Tidy this", "nit", line=4)
    first = first_round(r, [nit])
    finding = dict(residual(nit, "nit", root("f.py", 4, "Tidy this")), residual_root=None)
    refuses(r, "receipt_document_shape", second_round(r, first, findings=[finding], cells=cells(nit=1)),
            "carried root")


# C5: a finding with a file and no line.

@pytest.mark.parametrize("cell,key", [("block", "blocks_carried"), ("nit", "nits_carried")])
def test_a_file_with_no_line_keeps_one_id_across_rounds(ledger_rig, cell, key):
    r = ledger_rig
    raised = entry("Somewhere in this file", cell, path="src/app.py", line=None)
    first = first_round(r, [raised])
    ident = first.payload["findings"][0]["id"]
    assert ident.startswith("/unlocated/src/app.py:1#")
    count = cells(block=1) if cell == "block" else cells()
    event = write(r, second_round(r, first, cells=count, open_bugs=count["block"],
                                  **{key: [unlocated_copy(raised, key, "null")]}))
    assert event.payload["findings"] == [{"id": ident, "cell": cell, "carried_from": first.id}]


def test_a_missing_line_and_a_null_line_have_one_id(ledger_rig):
    r = ledger_rig
    raised = entry("Somewhere in this file", "nit", path="src/app.py", line=None)
    absent = {key: value for key, value in raised.items() if key != "line"}
    ids = [first_round(r, [value], lane=lane, served_model=model).payload["findings"][0]["id"]
           for value, lane, model in ((raised, "sol", "gpt-x"), (absent, "claude", "claude-opus-x"))]
    assert ids[0] == ids[1]


def test_the_same_file_and_description_twice_in_one_list_refuses(ledger_rig):
    raised = entry("Somewhere in this file", "nit", path="src/app.py", line=None)
    refuses(ledger_rig, "receipt_finding_duplicate",
            document(ledger_rig, findings=[raised, raised], cells=cells(nit=2)))


@pytest.mark.parametrize("path,line,name,detail", [
    ("", None, "receipt_document_shape", "finding locator"),
    ("src/app.py", 0, "receipt_document_shape", "finding locator"),
    ("src/app.py", "3", "receipt_document_shape", "finding locator"),
    (None, 3, "receipt_document_shape", "finding locator"),
    ("src/a pp.py", None, "receipt_document_shape", "finding locator"),
    ("src/a pp.py", 3, "receipt_finding_id_invalid", None),
], ids=["empty-file", "line-zero", "line-text", "line-without-file", "file-whitespace",
         "file-whitespace-with-line"])
def test_other_locator_shapes_still_refuse(ledger_rig, path, line, name, detail):
    raised = entry("Somewhere", "nit", path=path, line=line)
    refuses(ledger_rig, name, document(ledger_rig, findings=[raised], cells=cells(nit=1)), detail)


# L: no lighter receipt; every carried block is a block entry of the event.

def test_every_carried_block_is_a_block_entry_of_the_event(ledger_rig):
    r = ledger_rig
    bugs = [entry("First bug", "block", line=3), entry("Second bug", "block", path="g.py", line=5)]
    first = first_round(r, bugs)
    carried = [dict(bug, residual_root=root(bug["file"], bug["line"], bug["description"])) for bug in bugs]
    data = second_round(r, first, findings=[entry("New nit", "nit", line=7)], blocks_carried=carried,
                        cells=cells(block=2, nit=1), open_bugs=2)
    event = write(r, data)
    assert blocks(event) == event.payload["open_bugs"] == json.loads(data)["open_bugs"] == 2
