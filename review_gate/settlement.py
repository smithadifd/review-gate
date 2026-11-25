"""Pure sibling checks over verified, parsed receipt documents; no I/O.

Unlike the toolkit, families come from served_model, not lane names, and
output digests are shape-checked, not replayed. An adjudication may not resolve
carried findings; the toolkit honours resolved flags set by the seat. Group
settles and reparses refuse because their evidence is not in the ledger.
Path metadata is never opened or compared. This module is inert until the
records port calls it.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re

from . import ledger_records
from .refusal import refuse


SETTLEMENT = "receipt_settlement_invalid"
CHAIN = "receipt_sibling_chain_invalid"
ADJUDICATION = "receipt_adjudication_invalid"
SOURCES = {"settled_from", "adjudicated_from", "reparsed_from"}
RECORD = {"settled_from", "settled_by", "settled_at", "settle_reason", "settle_open"}
CHANGED = {"fixed_by_delta", "resolved_carried", "blocks_carried", "hardened_carried", "cells", "open_bugs"}
PATHS = {"receipt", "output_path", "raw_path", "result_path"}
LISTS = ("findings", "blocks_carried", "hardened_carried", "nits_carried", "resolved_carried", "fixed_by_delta")
FORMS = {"probed": ("probe-run", "lane-probe"), "quoted": ("quote",), "read": ("quote",)}
TURN = {"lane", "launch_kind", "served_model", "command", "red", "green", "output_sha256", "output_path"}
OPEN = {"prior", "root", "answer", "lane", "launch_kind", "served_model", "why", "output_sha256", "output_path"}
OID = re.compile(r"[0-9a-f]{40}")
SHA = re.compile(r"[0-9a-f]{64}")
ROOT = re.compile(r"[^\s:#]+:[1-9][0-9]*@[0-9a-f]{16}")


def _need(condition, event, why, name=SETTLEMENT):
    if not condition:
        refuse(name, f"{event.id} {why}")


def _comparable(value):
    if isinstance(value, dict):
        return {key: _comparable(entry) for key, entry in value.items() if key not in PATHS}
    if isinstance(value, list):
        return [_comparable(entry) for entry in value]
    return value


def _canonical(value):
    return json.dumps(_comparable(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _same(left, right):
    return _canonical(left) == _canonical(right)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _id(entry):
    if not isinstance(entry, dict):
        return None
    path, line, description = entry.get("file"), entry.get("line"), entry.get("description")
    if not (_text(path) and not any(c.isspace() for c in path)
            and _count(line) and line > 0 and isinstance(description, str)):
        return None
    return f"{path}:{line}@" + hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def _root(entry):
    root = entry.get("residual_root")
    return root if _matches(ROOT, root) else _id(entry)


def _strip(entry):
    return {key: value for key, value in entry.items() if key != "settle"}


def _entries(doc, event):
    for key in LISTS:
        entries = doc.get(key, [])
        _need(isinstance(entries, list), event, "entries")
        for entry in entries:
            if isinstance(entry, dict):
                yield entry


def _adjudication_carried(doc, source, event):
    keys = ("blocks_carried", "hardened_carried")
    _need(source["round"] < 2 or all(key in source for key in keys),
          event, "carried", ADJUDICATION)
    _need(all(_same(doc.get(key, []), source.get(key, [])) for key in keys)
          and _same(doc.get("resolved_carried", []), []), event, "carried", ADJUDICATION)


def _adjudication(doc, source, event):
    _need(source.get("verdict") == "unparseable", event, "source", ADJUDICATION)
    _need(doc.get("verdict") == "approved", event, "verdict", ADJUDICATION)
    _need(all(_text(doc.get(key)) for key in ("adjudicated_by", "adjudication_reason")),
          event, "record", ADJUDICATION)
    required = ("lane", "pr_repo", "pr", "reviewed_head", "round", "served_model",
                "requested_model", "tier", "counts_toward_cap", "changed_files_sha256", "base")
    _need(all(key in doc and key in source and _same(doc[key], source[key]) for key in required)
          and _same(doc.get("prior_receipt"), source.get("prior_receipt"))
          and ledger_records.family(doc.get("served_model")) is not None,
          event, "copied", ADJUDICATION)
    _adjudication_carried(doc, source, event)


def _partition(doc, source, event):
    for owner, key in ((source, "blocks_carried"), (doc, "blocks_carried"), (doc, "fixed_by_delta"),
                       (source, "resolved_carried"), (doc, "resolved_carried"),
                       (source, "hardened_carried"), (doc, "hardened_carried")):
        _need(isinstance(owner.get(key, []), list)
              and all(isinstance(e, dict) for e in owner.get(key, [])), event, "partition")
    fixed = doc.get("fixed_by_delta", [])
    _need(all(isinstance(e.get("settle"), dict) for e in fixed), event, "partition")
    moved = Counter(_canonical(_strip(e)) for e in fixed)
    kept = Counter(_canonical(e) for e in doc.get("blocks_carried", []))
    _need(Counter(_canonical(e) for e in source.get("blocks_carried", [])) == kept + moved,
          event, "partition")
    _need(_same(doc.get("hardened_carried", []), source.get("hardened_carried", [])),
          event, "partition")
    inherited = source.get("resolved_carried", [])
    resolved = doc.get("resolved_carried", [])
    _need(_same(resolved[:len(inherited)], inherited), event, "partition")
    available = Counter(_canonical(e) for e in source.get("hardened_carried", []))
    taken = Counter(_canonical(_strip(e)) for e in inherited)
    for entry in resolved[len(inherited):]:
        _need(isinstance(entry.get("settle"), dict), event, "partition")
        key = _canonical(_strip(entry))
        taken[key] += 1
        _need(taken[key] <= available[key], event, "partition")
    return [(True, entry) for entry in fixed] + [(False, entry) for entry in resolved[len(inherited):]]


def _path(value):
    if not (_text(value) and not value.startswith("/") and "\0" not in value
            and all(part not in ("", ".", "..") for part in value.split("/"))):
        return False
    try:
        value.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return False
    return True


def _selector(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    kind, _, rest = value.partition(":")
    if kind == "test":
        path, separator, name = rest.partition("::")
        return _path(path) and (not separator or (bool(name) and "::" not in name))
    if kind == "probe":
        parts = rest.split("/")
        return rest.startswith("/") and "settle" in parts[1:-1] and ".." not in parts and "" not in parts[1:]
    return False


def _fix_at(value, entry):
    if value is None:
        return True
    if not isinstance(value, str) or "," in value or any(c.isspace() for c in value):
        return False
    kind, _, rest = value.partition(":")
    if kind == "fix":
        return _path(rest) and rest != entry.get("file")
    path, separator, name = rest.partition("::")
    return kind == "test" and _path(path) and bool(separator and name) and "::" not in name


def _turn(record, event, why):
    # Launch kind and path metadata are present by schema, but carry no authority.
    _need(_text(record.get("lane")) and _text(record.get("launch_kind"))
          and ledger_records.family(record.get("served_model")) is not None
          and _matches(SHA, record.get("output_sha256")), event, why)


def _exits(record, event):
    command = record["command"]
    _need(_text(command) and command == command.strip() and "\n" not in command and "\r" not in command
          and type(record["red"]) is int and record["red"] != 0
          and type(record["green"]) is int and record["green"] == 0, event, "lane_probe")


def _lane_probe(record, settle, doc, event, block):
    keys = TURN | ({"fix_at"} if "fix_at" in settle else set())
    _need(isinstance(record, dict) and set(record) == keys | ({"second"} if block else set()),
          event, "lane_probe")
    _need("fix_at" not in settle or _same(record["fix_at"], settle["fix_at"]), event, "lane_probe")
    _exits(record, event)
    _turn(record, event, "lane_probe")
    raising = ledger_records.family(doc.get("served_model"))
    first = ledger_records.family(record["served_model"])
    _need(raising is not None and (not block or first == raising), event, "lane_probe")
    if block:
        second = record["second"]
        _need(isinstance(second, dict) and set(second) == keys, event, "lane_probe")
        _need("fix_at" not in settle or _same(second["fix_at"], settle["fix_at"]), event, "lane_probe")
        _exits(second, event)
        _turn(second, event, "lane_probe")
        _need(ledger_records.family(second["served_model"]) != first, event, "lane_probe")


def _probe(record, event):
    _need(isinstance(record, dict)
          and set(record) == {"selector", "method", "unfixed_tree", "head_tree", "red", "green"},
          event, "probe")
    _need(_selector(record["selector"]) and record["method"] in ("reverse-apply", "raised-head+tests")
          and all(_matches(OID, record[key]) for key in ("unfixed_tree", "head_tree")), event, "probe")
    for key in ("red", "green"):
        run = record[key]
        _need(isinstance(run, dict) and {"exit", "output_sha256"} <= set(run) <= {"exit", "output_sha256", "head_of_output"},
              event, "probe")
        _need(type(run["exit"]) is int and (run["exit"] != 0 if key == "red" else run["exit"] == 0)
              and _matches(SHA, run["output_sha256"])
              and ("head_of_output" not in run or isinstance(run["head_of_output"], str)), event, "probe")


def _quote(record, entry, doc, event):
    _need(isinstance(record, dict)
          and set(record) == {"prompt_sha256", "region", "quote", "answer", "requested_model", "served_model"},
          event, "quote")
    _need(_matches(SHA, record["prompt_sha256"]), event, "quote")
    region = record["region"]
    _need(isinstance(region, dict) and set(region) == {"file", "first", "last", "sha256"}
          and _same(region["file"], entry.get("file"))
          and _count(region["first"]) and region["first"] >= 1
          and _count(region["last"]) and region["last"] >= region["first"]
          and _matches(SHA, region["sha256"]), event, "quote")
    text, answer = record["quote"], record["answer"]
    _need(isinstance(text, str) and len(text) >= 20 and not text[:1].isspace()
          and isinstance(answer, str) and re.fullmatch(r"Settle: fixed — (\S.{18,}?)\s*", answer) is not None
          and text in answer and _text(record["requested_model"]), event, "quote")
    _need(ledger_records.family(record["served_model"]) is not None
          and ledger_records.family(record["served_model"]) == ledger_records.family(doc.get("served_model")),
          event, "quote")


def _moved(entry, block, doc, earlier, event):
    settle = entry["settle"]
    _need(_id(entry) is not None and _same(settle.get("prior"), _id(entry))
          and _same(settle.get("root"), _root(entry)), event, "unbound")
    evidence = entry.get("evidence")
    _need(isinstance(evidence, str) and settle.get("kind") in FORMS.get(evidence, ()), event, "form")
    _need(_matches(OID, settle.get("raised_head")) and any(
        candidate.get("lane") == doc.get("lane")
        and type(candidate.get("round")) is int and candidate["round"] < doc["round"]
        and candidate.get("reviewed_head") == settle["raised_head"]
        and isinstance(candidate.get("findings", []), list)
        and any(_id(f) == settle["root"] for f in candidate.get("findings", []))
        for candidate in earlier), event, "raised")
    hunks = settle.get("touched_hunks")
    _need(isinstance(hunks, list) and bool(hunks) and all(
        isinstance(pair, list) and len(pair) == 2 and all(
            isinstance(side, list) and len(side) == 2 and all(_count(n) for n in side)
            for side in pair) for pair in hunks), event, "hunks")
    kind = settle["kind"]
    form = {"probe-run": "probe", "lane-probe": "lane_probe", "quote": "quote"}[kind]
    located = {"fix_at"} if kind == "lane-probe" and "fix_at" in settle else set()
    _need(set(settle) == {"kind", "prior", "root", "raised_head", "touched_hunks", form} | located,
          event, form)
    _need(not located or _fix_at(settle["fix_at"], entry), event, "lane_probe")
    if kind == "probe-run":
        _probe(settle["probe"], event)
    elif kind == "quote":
        _quote(settle["quote"], entry, doc, event)
    else:
        _lane_probe(settle["lane_probe"], settle, doc, event, block)


def _open(doc, moved, event):
    if "settle_open" not in doc:
        return
    records = doc["settle_open"]
    _need(isinstance(records, list) and bool(records), event, "open")
    done = Counter(_canonical(_strip(e)) for e in doc.get("resolved_carried", []))
    carried = list(doc.get("blocks_carried", []))
    for entry in doc.get("hardened_carried", []):
        key = _canonical(entry)
        if done[key]:
            done[key] -= 1
        else:
            carried.append(entry)
    seen = set()
    for record in records:
        _need(isinstance(record, dict) and set(record) == OPEN, event, "open")
        prior = record["prior"]
        held = next((entry for entry in carried if _same(_id(entry), prior)), None)
        _need(held is not None and isinstance(prior, str) and prior not in seen
              and prior not in moved and _same(record["root"], _root(held)), event, "open")
        seen.add(prior)
        _turn(record, event, "open")
        block = any(_same(_id(entry), prior) for entry in doc.get("blocks_carried", []))
        raising = ledger_records.family(doc.get("served_model"))
        answering = ledger_records.family(record["served_model"])
        _need(raising is not None and (
            (record["answer"] == "open" and (not block or answering == raising))
            or (record["answer"] == "disagree" and block and answering != raising)), event, "open")
        why = record["why"]
        _need(_text(why) and "\n" not in why and "\r" not in why, event, "open")


def _settlement(doc, source, earlier, event):
    _need("settled_from" not in source and _same(source.get("fixed_by_delta", []), []),
          event, "chained")
    _need(("blind_violated" not in source or _same(source["blind_violated"], []))
          and _matches(OID, source.get("reconcile_base")), event, "not_blind")
    reason = doc.get("settle_reason")
    _need(all(_text(doc.get(key)) for key in ("settled_by", "settled_at"))
          and isinstance(reason, str) and len(reason) >= 20 and not reason[:1].isspace(), event, "record")
    _need(ledger_records.family(source.get("served_model")) is not None
          and all((key in doc) == (key in source) and _same(doc.get(key), source.get(key))
                  for key in (set(doc) | set(source)) - RECORD - CHANGED - PATHS), event, "copied")
    _need(source.get("verdict") in ("approved", "changes_requested"), event, "source")
    moved = _partition(doc, source, event)
    for block, entry in moved:
        _moved(entry, block, doc, earlier, event)
    _open(doc, {entry["settle"]["prior"] for _, entry in moved}, event)
    cells, original = doc.get("cells"), source.get("cells")
    _need(isinstance(cells, dict) and isinstance(original, dict)
          and set(cells) == {"block", "harden", "nit"} and all(_count(n) for n in cells.values())
          and isinstance(doc.get("findings"), list)
          and _same(cells["block"], sum(e.get("cell") == "block" for e in doc["findings"])
                    + len(doc.get("blocks_carried", [])))
          and all(_same(cells[key], original.get(key)) for key in ("harden", "nit"))
          and _count(doc.get("open_bugs")) and _same(doc["open_bugs"], cells["block"]), event, "cells")


def check_slot(events, documents, earlier_documents=()):
    """Return the newest event, or raise Refusal with ``<id> <why>`` detail.

    ``events`` are one slot's receipt events in ascending id order, exposing
    ``id`` and ``payload`` (supersedes, doc_sha256, findings). ``documents``
    maps each event id to its verified parsed document. ``earlier_documents``
    contains earlier verified documents on the same item for raised-root lookup,
    where lane and round are checked again. Carried lists are checked only
    against the slot's source, not re-derived across rounds. The caller verifies
    document bytes and event/document findings consistency, not this check.

    Grammar: plain; plain/adjudicated; plain/settled;
    plain/adjudicated/settled. Content checks constrain their verdicts.
    The optional earlier iterable is consumed once; no input is modified.
    """
    events = tuple(events)
    if not events:
        refuse(CHAIN, "0 grammar")
    last = events[-1]
    _need(len(events) <= 3, last, "grammar", CHAIN)
    earlier = tuple(earlier_documents)
    previous = None
    for event in events:
        _need(event.id in documents and isinstance(documents[event.id], dict), event, "document", CHAIN)
        doc = documents[event.id]
        _need("reparsed_from" not in doc, event, "reparse", "receipt_reparse_unsupported")
        _need(not any(isinstance(entry.get("settle"), dict)
                      and ("group" in entry["settle"] or entry["settle"].get("kind") == "group-member")
                      for entry in _entries(doc, event)),
              event, "group", "receipt_settlement_group_unsupported")
        sources = set(doc) & SOURCES
        kind = "settled" if "settled_from" in sources else "adjudicated" if sources else "plain"
        _need((previous is None and "supersedes" not in event.payload and not sources)
              or (previous is not None and type(event.payload.get("supersedes")) is int
                  and event.payload["supersedes"] == previous.id and event.id > previous.id
                  and sources in ({"settled_from"}, {"adjudicated_from"},
                                  {"settled_from", "adjudicated_from"})), event, "grammar", CHAIN)
        if previous is not None:
            source_key = "settled_from" if kind == "settled" else "adjudicated_from"
            link = doc[source_key]
            _need(isinstance(link, dict) and _matches(SHA, link.get("sha256"))
                  and _same(link["sha256"], previous.payload.get("doc_sha256")), event, "digest", CHAIN)
            source = documents[previous.id]
            if kind == "adjudicated":
                _adjudication(doc, source, event)
            else:
                if "adjudicated_from" in sources:
                    link = doc["adjudicated_from"]
                    _need(isinstance(link, dict) and _matches(SHA, link.get("sha256"))
                          and _same(link["sha256"], events[0].payload.get("doc_sha256")),
                          event, "digest", CHAIN)
                _settlement(doc, source, earlier, event)
        previous = event
    deciding = documents[last.id]
    _need("settled_from" in deciding or (
        _same(deciding.get("fixed_by_delta", []), [])
        and not any("settle" in entry for entry in _entries(deciding, last))), last, "unbound")
    return last
