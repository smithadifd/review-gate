"""Synthetic, document-only sibling grammar and evidence checks."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from review_gate.refusal import Refusal
from review_gate.settlement import check_slot


HEAD = "a" * 40
RAISED = "b" * 40
DIGEST = "c" * 64
MODEL = "openai/gpt-test"
OTHER = "anthropic/claude-test"


def ident(entry):
    return f"{entry['file']}:{entry['line']}@" + hashlib.sha256(entry['description'].encode()).hexdigest()[:16]


def finding(cell="block", evidence="probed"):
    return dict(file="sample.py", line=3, description="Synthetic finding", cell=cell, evidence=evidence)


def plain(**extra):
    doc = dict(lane="lane-a", round=2, pr_repo="example/repo", pr=7,
               reviewed_head=HEAD, served_model=MODEL, requested_model=MODEL,
               tier="guard", counts_toward_cap=True, changed_files_sha256=DIGEST,
               base=RAISED, prior_receipt={"sha256": DIGEST}, reconcile_base=RAISED,
               verdict="approved", findings=[], hardened_carried=[], blocks_carried=[],
               resolved_carried=[], cells=dict(block=0, harden=0, nit=0), open_bugs=0)
    doc.update(extra)
    return doc


def digest(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def adjudicate(source):
    return dict(deepcopy(source), verdict="approved", adjudicated_from={"sha256": digest(source)},
                adjudicated_by="adjudicator", adjudication_reason="The output records an approval.")


def turn(model=MODEL, located=False):
    result = dict(lane="arbitrary-lane", launch_kind="arbitrary-launch", served_model=model,
                  command="pytest tests/test_sample.py", red=1, green=0,
                  output_sha256=DIGEST, output_path="unread-output")
    if located:
        result["fix_at"] = None
    return result


def settle_record(entry, kind="lane-probe", located=False):
    record = dict(kind=kind, prior=ident(entry), root=entry.get("residual_root", ident(entry)),
                  raised_head=RAISED, touched_hunks=[[[1, 2], [1, 3]]])
    if kind == "lane-probe":
        record["lane_probe"] = turn(located=located)
        if entry["cell"] == "block":
            record["lane_probe"]["second"] = turn(OTHER, located)
        if located:
            record["fix_at"] = None
    elif kind == "probe-run":
        record["probe"] = dict(selector="test:tests/test_sample.py::test_fixed", method="reverse-apply",
                               unfixed_tree=RAISED, head_tree=HEAD,
                               red={"exit": 1, "output_sha256": DIGEST},
                               green={"exit": 0, "output_sha256": DIGEST})
    else:
        text = "The documented behavior now matches."
        record["quote"] = dict(prompt_sha256=DIGEST,
                               region=dict(file=entry["file"], first=1, last=3, sha256=DIGEST),
                               quote=text, answer="Settle: fixed — " + text,
                               requested_model=MODEL, served_model=MODEL)
    return record


def settled(source, kind="lane-probe", located=False):
    doc = dict(deepcopy(source), settled_from={"sha256": digest(source)},
               settled_by="settler", settled_at="2026-01-01T00:00:00Z",
               settle_reason="Recorded evidence establishes the fix.")
    doc["fixed_by_delta"] = [dict(deepcopy(e), settle=settle_record(e, kind, located))
                             for e in source.get("blocks_carried", [])]
    doc["blocks_carried"] = []
    doc["resolved_carried"] = deepcopy(source.get("resolved_carried", [])) + [
        dict(deepcopy(e), settle=settle_record(e, kind, located))
        for e in source.get("hardened_carried", [])
        if e not in source.get("resolved_carried", [])]
    doc["cells"]["block"] = sum(e["cell"] == "block" for e in doc["findings"])
    doc["open_bugs"] = doc["cells"]["block"]
    return doc


def context(cell="block", evidence="probed", kind="lane-probe", located=False):
    entry = finding(cell, evidence)
    source = plain(**{"blocks_carried" if cell == "block" else "hardened_carried": [entry]})
    source["cells"]["block"] = source["open_bugs"] = int(cell == "block")
    earlier = [dict(plain(), round=1, reviewed_head=RAISED, findings=[entry])]
    return source, settled(source, kind, located), earlier


def run(docs, earlier=()):
    events = []
    documents = {}
    for index, doc in enumerate(docs, 1):
        payload = dict(doc_sha256=digest(doc), findings=deepcopy(doc.get("findings", [])))
        if events:
            payload["supersedes"] = events[-1].id
        event = SimpleNamespace(id=index, payload=payload)
        events.append(event)
        documents[index] = doc
    return check_slot(events, documents, earlier), events


def refuses(docs, why, name="receipt_settlement_invalid", earlier=()):
    with pytest.raises(Refusal) as error:
        run(docs, earlier)
    assert error.value.name == name, error.value.line()
    assert error.value.detail == f"{len(docs)} {why}", error.value.line()


@pytest.mark.parametrize("grammar", [0, 1, 2, 3])
def test_valid_grammar_decides_newest(grammar):
    source, final, earlier = context()
    if grammar == 0:
        docs = [plain()]
    elif grammar == 1:
        original = plain(verdict="unparseable", open_bugs=None)
        docs = [original, adjudicate(original)]
    elif grammar == 2:
        docs = [source, final]
    else:
        original = dict(source, verdict="unparseable", open_bugs=None)
        approved = adjudicate(original)
        docs = [original, approved, settled(approved)]
    decision, events = run(docs, earlier)
    assert decision is events[-1]


@pytest.mark.parametrize("cell,evidence,kind,located", [
    ("block", "probed", "lane-probe", False),
    ("block", "probed", "lane-probe", True),
    ("harden", "probed", "lane-probe", False),
    ("harden", "probed", "lane-probe", True),
    ("block", "probed", "probe-run", False),
    ("harden", "probed", "probe-run", False),
    ("block", "quoted", "quote", False),
    ("harden", "read", "quote", False),
])
def test_settle_forms(cell, evidence, kind, located):
    source, final, earlier = context(cell, evidence, kind, located)
    before = deepcopy((source, final, earlier))
    decision, events = run([source, final], iter(earlier))
    assert decision is events[-1]
    assert (source, final, earlier) == before


def test_changes_requested_settlement_keeps_own_findings():
    source, _, earlier = context()
    source.update(verdict="changes_requested", findings=[dict(finding(), line=8)],
                  cells=dict(block=2, harden=0, nit=0), open_bugs=2)
    final = settled(source)
    decision, events = run([source, final], earlier)
    assert decision is events[-1]
    assert final["verdict"] == "changes_requested"
    assert final["open_bugs"] == 1


@pytest.mark.parametrize("field,value", [
    ("verdict", "approved"), ("findings", []), ("counts_toward_cap", False),
    ("served_model", OTHER), ("tier", "light"), ("base", HEAD),
    ("lane", "lane-b"), ("round", 3), ("requested_model", OTHER),
    ("changed_files_sha256", "d" * 64), ("prior_receipt", {"sha256": "d" * 64}),
    ("extra", True),
])
def test_copied_fields_cannot_change(field, value):
    source, _, earlier = context()
    source.update(verdict="changes_requested", findings=[dict(finding(), line=8)],
                  cells=dict(block=2, harden=0, nit=0), open_bugs=2)
    final = settled(source)
    final[field] = value
    refuses([source, final], "copied", earlier=earlier)


def test_canonical_boolean_is_not_integer():
    source, _, earlier = context()
    source["extension"] = {"nested": True}
    final = settled(source)
    final["extension"] = {"nested": 1}
    refuses([source, final], "copied", earlier=earlier)


def test_canonical_integer_is_not_float():
    source, _, earlier = context()
    source["extension"] = 1
    final = settled(source)
    final["extension"] = 1.0
    refuses([source, final], "copied", earlier=earlier)


def test_path_metadata_is_neither_compared_nor_opened():
    source, _, earlier = context()
    source.update(result_path="missing-source", raw_path="missing-raw")
    final = settled(source)
    final.update(result_path="missing-result", raw_path="other-raw")
    final["settled_from"]["receipt"] = "missing-receipt"
    final["fixed_by_delta"][0]["settle"]["lane_probe"]["output_path"] = {"not": "a path"}
    assert run([source, final], earlier)[0].id == 2


@pytest.mark.parametrize("field,value", [
    ("adjudicated_by", ""), ("adjudication_reason", None),
    ("verdict", "changes_requested"),
])
def test_adjudication_record_and_verdict(field, value):
    source = plain(verdict="unparseable", open_bugs=None)
    final = adjudicate(source)
    final[field] = value
    refuses([source, final], "verdict" if field == "verdict" else "record",
            "receipt_adjudication_invalid")


def test_adjudication_cannot_override_approved():
    source = plain()
    refuses([source, adjudicate(source)], "source", "receipt_adjudication_invalid")


@pytest.mark.parametrize("field", [
    "served_model", "requested_model", "tier", "counts_toward_cap", "changed_files_sha256",
    "base", "prior_receipt", "lane", "pr_repo", "pr", "reviewed_head", "round",
])
def test_adjudication_copied_fields(field):
    source = plain(verdict="unparseable", open_bugs=None)
    final = adjudicate(source)
    del final[field]
    refuses([source, final], "copied", "receipt_adjudication_invalid")


def test_adjudication_another_model():
    source = plain(verdict="unparseable", open_bugs=None)
    final = adjudicate(source)
    final["served_model"] = OTHER
    refuses([source, final], "copied", "receipt_adjudication_invalid")


def test_unknown_adjudication_family():
    source = plain(verdict="unparseable", open_bugs=None, served_model="unknown")
    refuses([source, adjudicate(source)], "copied", "receipt_adjudication_invalid")


@pytest.mark.parametrize("which", ["settled_from", "adjudicated_from"])
def test_source_digest_must_bind_event(which):
    source, final, earlier = context()
    if which == "adjudicated_from":
        source = plain(verdict="unparseable", open_bugs=None)
        final = adjudicate(source)
    final[which]["sha256"] = DIGEST
    refuses([source, final], "digest", "receipt_sibling_chain_invalid", earlier)


def test_settled_adjudication_keeps_original_digest():
    source, _, earlier = context()
    original = dict(source, verdict="unparseable", open_bugs=None)
    approved = adjudicate(original)
    final = settled(approved)
    final["adjudicated_from"]["sha256"] = digest(approved)
    refuses([original, approved, final], "digest", "receipt_sibling_chain_invalid", earlier)


def test_third_sibling_refuses():
    source, final, earlier = context()
    original = dict(source, verdict="unparseable", open_bugs=None)
    approved = adjudicate(original)
    final = settled(approved)
    refuses([original, approved, final, settled(final)], "grammar", "receipt_sibling_chain_invalid", earlier)


def test_unknown_chain_refuses():
    refuses([plain(), plain()], "grammar", "receipt_sibling_chain_invalid")


def test_unknown_source_combination_refuses():
    source, final, earlier = context()
    final["adjudicated_from"] = {"sha256": digest(source)}
    refuses([source, final], "copied", earlier=earlier)


def test_orphan_sibling_refuses():
    _, final, earlier = context()
    refuses([final], "grammar", "receipt_sibling_chain_invalid", earlier)


def test_empty_slot_refuses():
    with pytest.raises(Refusal, match="receipt_sibling_chain_invalid: 0 grammar"):
        check_slot([], {})


@pytest.mark.parametrize("fault", ["document", "supersedes", "order"])
def test_event_chain_binding(fault):
    source, final, earlier = context()
    events = [SimpleNamespace(id=1, payload={"doc_sha256": digest(source)}),
              SimpleNamespace(id=2, payload={"doc_sha256": digest(final), "supersedes": 1})]
    documents = {1: source, 2: final}
    if fault == "document":
        del documents[2]
    elif fault == "supersedes":
        events[1].payload["supersedes"] = 9
    else:
        events.reverse()
    with pytest.raises(Refusal) as error:
        check_slot(events, documents, earlier)
    assert error.value.name == "receipt_sibling_chain_invalid"


@pytest.mark.parametrize("field,value,why", [
    ("blind_violated", ["own finding"], "not_blind"),
    ("blind_violated", None, "not_blind"),
    ("reconcile_base", None, "not_blind"),
    ("fixed_by_delta", [finding()], "chained"),
    ("verdict", "unparseable", "source"),
])
def test_settlement_source_rules(field, value, why):
    source, _, earlier = context()
    source[field] = value
    final = settled(source)
    refuses([source, final], why, earlier=earlier)


def test_settlement_cannot_settle_settlement():
    source, final, earlier = context()
    refuses([source, final, settled(final)], "chained", earlier=earlier)


@pytest.mark.parametrize("field,value", [
    ("settled_by", ""), ("settled_at", None), ("settle_reason", "too short"),
    ("settle_reason", " " + "x" * 30),
])
def test_settlement_record(field, value):
    source, final, earlier = context()
    final[field] = value
    refuses([source, final], "record", earlier=earlier)


@pytest.mark.parametrize("fault", [
    "lost-block", "missing-record", "changed-carried", "bad-list", "harden-record",
    "harden-not-available", "harden-twice", "harden-changed", "inherited",
])
def test_partition(fault):
    source, final, earlier = context("harden" if fault.startswith("harden") or fault == "inherited" else "block")
    if fault == "lost-block":
        final["fixed_by_delta"] = []
    elif fault == "missing-record":
        del final["fixed_by_delta"][0]["settle"]
    elif fault == "changed-carried":
        final["fixed_by_delta"][0]["description"] = "different"
    elif fault == "bad-list":
        final["blocks_carried"] = {}
    elif fault == "harden-record":
        del final["resolved_carried"][0]["settle"]
    elif fault == "harden-not-available":
        final["resolved_carried"][0]["line"] = 8
    elif fault == "harden-twice":
        final["resolved_carried"] *= 2
    elif fault == "harden-changed":
        final["hardened_carried"] = []
    else:
        source["resolved_carried"] = deepcopy(source["hardened_carried"])
        final["settled_from"]["sha256"] = digest(source)
    refuses([source, final], "partition", earlier=earlier)


def alter(value, path, replacement):
    keys = path.split(".")
    for key in keys[:-1]:
        value = value[int(key)] if isinstance(value, list) else value[key]
    key = int(keys[-1]) if isinstance(value, list) else keys[-1]
    if replacement is DELETE:
        del value[key]
    else:
        value[key] = replacement


DELETE = object()


@pytest.mark.parametrize("path,value,why", [
    ("prior", "unknown", "unbound"), ("root", "unknown", "unbound"),
    ("kind", "unknown", "form"), ("raised_head", "bad", "raised"),
    ("touched_hunks", [], "hunks"), ("touched_hunks", [[[True, 0], [1, 2]]], "hunks"),
    ("extra", "unknown", "lane_probe"), ("lane_probe.extra", 1, "lane_probe"),
    ("lane_probe.second", DELETE, "lane_probe"),
    ("lane_probe.second.extra", 1, "lane_probe"),
    ("lane_probe.second.served_model", MODEL, "lane_probe"),
    ("lane_probe.served_model", OTHER, "lane_probe"),
    ("lane_probe.served_model", "unknown", "lane_probe"),
    ("lane_probe.second.served_model", "unknown", "lane_probe"),
    ("lane_probe.output_sha256", "bad", "lane_probe"),
    ("lane_probe.second.output_sha256", "bad", "lane_probe"),
    ("lane_probe.lane", "", "lane_probe"), ("lane_probe.launch_kind", "", "lane_probe"),
    ("lane_probe.command", " command ", "lane_probe"),
    ("lane_probe.command", "a\nb", "lane_probe"),
    ("lane_probe.red", 0, "lane_probe"), ("lane_probe.green", 1, "lane_probe"),
    ("lane_probe.red", True, "lane_probe"),
    ("lane_probe.second.green", False, "lane_probe"),
])
def test_moved_record(path, value, why):
    source, final, earlier = context()
    alter(final["fixed_by_delta"][0]["settle"], path, value)
    refuses([source, final], why, earlier=earlier)


@pytest.mark.parametrize("path,value", [
    ("fix_at", False), ("fix_at", "fix:sample.py"), ("fix_at", "fix:../other.py"),
    ("fix_at", "test:tests/test_sample.py"), ("fix_at", "test:x.py::a::b"),
    ("fix_at", "fix:other.py,fix:another.py"),
    ("lane_probe.fix_at", False), ("lane_probe.second.fix_at", False),
])
def test_located_record(path, value):
    source, final, earlier = context(located=True)
    alter(final["fixed_by_delta"][0]["settle"], path, value)
    if path == "fix_at":
        record = final["fixed_by_delta"][0]["settle"]
        record["lane_probe"]["fix_at"] = record["lane_probe"]["second"]["fix_at"] = value
    refuses([source, final], "lane_probe", earlier=earlier)


@pytest.mark.parametrize("fix_at", ["fix:other.py", "test:tests/test_sample.py::test_fixed"])
def test_located_fix_suffixes(fix_at):
    source, final, earlier = context(located=True)
    record = final["fixed_by_delta"][0]["settle"]
    record["fix_at"] = record["lane_probe"]["fix_at"] = record["lane_probe"]["second"]["fix_at"] = fix_at
    assert run([source, final], earlier)[0].id == 2


@pytest.mark.parametrize("fault", ["absent", "lane", "round", "head", "root"])
def test_root_must_have_been_raised(fault):
    source, final, earlier = context()
    if fault == "absent":
        earlier = []
    elif fault == "lane":
        earlier[0]["lane"] = "lane-b"
    elif fault == "round":
        earlier[0]["round"] = 2
    elif fault == "head":
        earlier[0]["reviewed_head"] = HEAD
    else:
        earlier[0]["findings"] = []
    refuses([source, final], "raised", earlier=earlier)


@pytest.mark.parametrize("path,value", [
    ("extra", 1), ("selector", "test:../test.py"), ("selector", "Test:test.py"),
    ("selector", "test: test.py"), ("selector", "test:test.py::"),
    ("method", "unknown"), ("unfixed_tree", "bad"),
    ("red.extra", 1), ("red.exit", 0), ("green.exit", 1),
    ("red.exit", True), ("green.exit", False),
    ("red.output_sha256", "bad"), ("green.head_of_output", 1),
])
def test_probe_shape(path, value):
    source, final, earlier = context(kind="probe-run")
    alter(final["fixed_by_delta"][0]["settle"]["probe"], path, value)
    refuses([source, final], "probe", earlier=earlier)


def test_absolute_probe_selector_is_not_opened():
    source, final, earlier = context(kind="probe-run")
    final["fixed_by_delta"][0]["settle"]["probe"]["selector"] = "probe:/scratch/settle/probe.sh"
    assert run([source, final], earlier)[0].id == 2


@pytest.mark.parametrize("path,value", [
    ("extra", 1), ("prompt_sha256", "bad"), ("region.file", "other.py"),
    ("region.first", True), ("region.last", 0), ("region.sha256", "bad"),
    ("quote", "short"), ("answer", "Settle: fixed - Wrong punctuation"),
    ("requested_model", ""), ("served_model", OTHER), ("served_model", "unknown"),
])
def test_quote_shape(path, value):
    source, final, earlier = context(evidence="quoted", kind="quote")
    alter(final["fixed_by_delta"][0]["settle"]["quote"], path, value)
    refuses([source, final], "quote", earlier=earlier)


@pytest.mark.parametrize("group", ["parent", "member"])
def test_group_refuses_by_name(group):
    source, final, earlier = context()
    record = final["fixed_by_delta"][0]["settle"]
    if group == "parent":
        record["group"] = {}
    else:
        record["kind"] = "group-member"
    refuses([source, final], "group", "receipt_settlement_group_unsupported", earlier)


def test_reparse_refuses_by_name():
    source = plain()
    final = dict(source, reparsed_from={"sha256": digest(source)})
    refuses([source, final], "reparse", "receipt_reparse_unsupported")


@pytest.mark.parametrize("kind", ["fixed", "record"])
def test_settlement_evidence_needs_source(kind):
    source = plain()
    if kind == "fixed":
        source["fixed_by_delta"] = [finding()]
    else:
        source["hardened_carried"] = [dict(finding("harden"), settle={})]
    refuses([source], "unbound")


@pytest.mark.parametrize("field,value", [
    ("cells", {}), ("cells.block", 1), ("cells.harden", 1), ("cells.nit", True),
    ("open_bugs", 1), ("open_bugs", False),
])
def test_cells_are_recomputed(field, value):
    source, final, earlier = context()
    alter(final, field, value)
    refuses([source, final], "cells", earlier=earlier)


def open_context(cell="block", answer="open", model=MODEL):
    entry = finding(cell)
    source = plain(**{"blocks_carried" if cell == "block" else "hardened_carried": [entry]})
    source["cells"]["block"] = source["open_bugs"] = int(cell == "block")
    final = dict(deepcopy(source), settled_from={"sha256": digest(source)}, settled_by="settler",
                 settled_at="2026-01-01", settle_reason="The recorded answer remains open.")
    record = dict(prior=ident(entry), root=ident(entry), answer=answer, lane="any-lane",
                  launch_kind="any-kind", served_model=model, why="Still open.",
                  output_sha256=DIGEST, output_path="unread-output")
    final["settle_open"] = [record]
    return source, final


@pytest.mark.parametrize("cell,answer,model", [
    ("block", "open", MODEL), ("block", "disagree", OTHER),
    ("harden", "open", OTHER),
])
def test_open_answers(cell, answer, model):
    source, final = open_context(cell, answer, model)
    assert run([source, final])[0].id == 2


@pytest.mark.parametrize("path,value", [
    ("extra", 1), ("prior", "missing"), ("root", "missing"),
    ("answer", "fixed"), ("served_model", "unknown"),
    ("served_model", OTHER), ("answer", "disagree"),
    ("why", ""), ("why", "one\ntwo"), ("output_sha256", "bad"),
])
def test_open_record(path, value):
    source, final = open_context()
    alter(final["settle_open"][0], path, value)
    refuses([source, final], "open")


@pytest.mark.parametrize("fault", ["empty", "duplicate", "resolved-harden", "harden-disagree"])
def test_open_partition(fault):
    source, final = open_context("harden" if fault.startswith("harden") or fault == "resolved-harden" else "block")
    if fault == "empty":
        final["settle_open"] = []
    elif fault == "duplicate":
        final["settle_open"] *= 2
    elif fault == "resolved-harden":
        source["resolved_carried"] = deepcopy(source["hardened_carried"])
        final["resolved_carried"] = deepcopy(source["resolved_carried"])
        final["settled_from"]["sha256"] = digest(source)
    else:
        final["settle_open"][0].update(answer="disagree", served_model=OTHER)
    refuses([source, final], "open")


def test_absent_carried_lists_mean_empty():
    source = plain()
    del source["blocks_carried"], source["hardened_carried"], source["resolved_carried"]
    assert run([source, settled(source)])[0].id == 2


def test_unknown_source_family_refuses_even_without_moves():
    source = plain(served_model="unknown")
    refuses([source, settled(source)], "copied")


def test_probe_selector_rejects_nul():
    source, final, earlier = context(kind="probe-run")
    final["fixed_by_delta"][0]["settle"]["probe"]["selector"] = "test:bad\0path.py"
    refuses([source, final], "probe", earlier=earlier)


def test_block_first_turn_must_be_raising_family():
    source, final, earlier = context()
    record = final["fixed_by_delta"][0]["settle"]["lane_probe"]
    record["served_model"] = OTHER
    record["second"]["served_model"] = MODEL
    refuses([source, final], "lane_probe", earlier=earlier)
