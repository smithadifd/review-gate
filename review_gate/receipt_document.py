"""Derive receipt fields from the exact bytes the ledger will attach."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re

from .grammar import parse_pr_url
from .ledger_records import family
from .records import waiver_grant_id
from .refusal import refuse

CELLS = ("block", "harden", "nit")
DIGEST = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
ROOT = re.compile(r"[^\s:#]+:[1-9][0-9]*@[0-9a-f]{16}")
TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
CAP = 65536
SOURCES = ("settled_from", "adjudicated_from", "reparsed_from")
# Each carried list's cell: an entry of the list takes it, and one recorded in
# another cell refuses.
LIST_CELLS = {"hardened_carried": "harden", "blocks_carried": "block", "nits_carried": "nit"}
# Entries on one id across lists merge into the most severe cell.
SEVERITY = {"nit": 0, "harden": 1, "block": 2}


def parse_document(data):
    if type(data) is not bytes:
        refuse("receipt_doc_not_json", "the document must be bytes")
    if len(data) > CAP:
        refuse("receipt_doc_too_large", f"the inline limit is {CAP} bytes")

    def pairs(entries):
        value = {}
        for key, item in entries:
            if key in value:
                raise ValueError("repeated key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("non-finite number")

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite number")
        return number

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant, parse_float=finite)
        pending = [value]
        while pending:
            current = pending.pop()
            if type(current) is str:
                current.encode("utf-8")
            elif type(current) is dict:
                pending.extend(current.keys())
                pending.extend(current.values())
            elif type(current) is list:
                pending.extend(current)
    except (ValueError, UnicodeError, RecursionError):
        refuse("receipt_doc_not_json", "expected one UTF-8 JSON object without repeated keys")
    if type(value) is not dict:
        refuse("receipt_doc_not_json", "expected an object")
    return value


def _shape(condition, key):
    if not condition:
        refuse("receipt_document_shape", key)


def document_digest(value, key):
    _shape(type(value) is dict and type(value.get("sha256")) is str
           and DIGEST.fullmatch(value["sha256"]) is not None, key)
    return value["sha256"]


def finding_id(entry):
    """One locator and a stable root over the first-raise toolkit id text."""
    _shape(type(entry) is dict and type(entry.get("description")) is str, "finding")
    path, line = entry.get("file"), entry.get("line")
    if path is None and line is None:
        path, line = "/unlocated", 1
    elif line is None and type(path) is str and path and not any(c.isspace() for c in path):
        # A file with no line: the id binds the file and the description, never a
        # position, so a finding and its carried copy keep one id. A file with
        # whitespace keeps v0.2.0's refusal.
        path, line = f"/unlocated/{path}", 1
    _shape(type(path) is str and bool(path) and type(line) is int and line >= 1, "finding locator")
    try:
        waiver_grant_id("example/repo", 1, f"{path}:{line}#root")
    except ValueError:
        refuse("receipt_finding_id_invalid", "the locator cannot name a waiver")
    description = hashlib.sha256(entry["description"].encode("utf-8")).hexdigest()[:16]
    first = entry.get("residual_root")
    if first is None:
        first = f"{path}:{line}@{description}"
    _shape(type(first) is str and ROOT.fullmatch(first) is not None, "residual_root")
    root = hashlib.sha256(first.encode("utf-8")).hexdigest()[:16]
    ident = f"{path}:{line}#{root}"
    if len(ident) > 300 or re.fullmatch(r"[^\s#]+:[1-9][0-9]*#[^\s#]+", ident) is None:
        refuse("receipt_finding_id_invalid", "the locator exceeds the ledger grammar")
    return ident


def _located_key(entry):
    """(file, line, description) of a located entry, else None: the reader's resolved match."""
    if type(entry) is not dict:
        return None
    path, line, description = entry.get("file"), entry.get("line"), entry.get("description")
    if type(path) is str and type(line) is int and line >= 1 and type(description) is str:
        return path, line, description
    return None


@dataclass(frozen=True)
class DerivedReceipt:
    document: dict
    fields: dict
    carried: tuple
    prior: str | None
    source: str | None
    adjudicated: str | None = None


def derive_document(data):
    """Derive decided fields once; links remain document digests until lookup."""
    doc = parse_document(data)
    repo, number = doc.get("pr_repo"), doc.get("pr")
    if repo is None:
        refuse("receipt_repository_missing", "pr_repo is required")
    _shape(type(repo) is str and type(number) is int and number > 0, "pull request")
    pr = parse_pr_url(f"https://github.com/{repo}/pull/{number}")
    fields = {"repo": pr.key, "pr": number}
    for source, target, pattern in (("reviewed_head", "head", COMMIT),
                                    ("changed_files_sha256", "changed_files_sha256", DIGEST),
                                    ("lane", "lane", TOKEN), ("tier", "tier", TOKEN)):
        value = doc.get(source)
        _shape(type(value) is str and pattern.fullmatch(value) is not None, source)
        fields[target] = value
    _shape(type(doc.get("round")) is int and doc["round"] >= 1, "round")
    _shape(type(doc.get("counts_toward_cap")) is bool, "counts_toward_cap")
    fields.update(round=doc["round"], counts_toward_cap=doc["counts_toward_cap"])
    requested = doc.get("requested_model")
    _shape(type(requested) is str and 0 < len(requested) <= 200, "requested_model")
    fields["requested_model"] = requested
    verdict = doc.get("verdict")
    _shape(verdict in ("approved", "changes_requested", "unparseable", "crashed"), "verdict")
    fields["verdict"] = verdict
    model = doc.get("served_model")
    if model is not None:
        _shape(type(model) is str and 0 < len(model) <= 200, "served_model")
        derived_family = family(model) or "unknown"
        if "family" in doc and doc["family"] != derived_family:
            refuse("receipt_family_mismatch", "family differs from served_model")
        fields.update(served_model=model, family=derived_family)
    elif "family" in doc:
        refuse("receipt_family_mismatch", "family has no served_model")
    prior = document_digest(doc.get("prior_receipt"), "prior_receipt") if fields["round"] >= 2 else None
    _shape(fields["round"] >= 2 or "prior_receipt" not in doc, "prior_receipt at round 1")
    # One source, or a settled sibling of an adjudication: settling copies the
    # adjudication whole, so it keeps adjudicated_from and adds settled_from, the
    # source it supersedes. Any other pair, and all three, refuse.
    sources = [key for key in SOURCES if key in doc]
    _shape(len(sources) <= 1 or sources == ["settled_from", "adjudicated_from"], "sibling source")
    source = document_digest(doc[sources[0]], sources[0]) if sources else None
    adjudicated = document_digest(doc["adjudicated_from"], "adjudicated_from") if len(sources) == 2 else None
    carried = []
    if verdict in ("approved", "changes_requested"):
        _shape(model is not None and doc.get("severity_rubric") == "v2", "counted document")
        for key in ("findings", "hardened_carried"):
            _shape(type(doc.get(key)) is list, key)
        for key in ("blocks_carried", "nits_carried", "resolved_carried", "standing_carried", "unsettled_carried"):
            _shape(key not in doc or type(doc[key]) is list, key)
        # The reader's resolved match: located entries only, exact (file, line, description).
        resolved = {_located_key(fixed) for fixed in doc.get("resolved_carried", [])} - {None}
        own = dict.fromkeys(CELLS, 0)
        merged = {}
        for key in ("findings", "hardened_carried", "blocks_carried", "nits_carried"):
            listed = set()
            for entry in doc.get(key, []):
                _shape(type(entry) is dict, key)
                cell = entry.get("cell")
                if cell not in CELLS:
                    refuse("receipt_cell_unknown", "a finding has no known cell")
                if key == "hardened_carried" and _located_key(entry) in resolved:
                    continue
                listed_cell = LIST_CELLS.get(key, cell)
                if cell != listed_cell:
                    refuse("receipt_document_inconsistent", f"a {key} entry is not in the {listed_cell} cell")
                if key == "findings":
                    own[cell] += 1
                ident = finding_id(entry)
                if ident in listed:
                    refuse("receipt_finding_duplicate", ident)
                listed.add(ident)
                is_carried = key != "findings" or entry.get("residual_of") is not None
                if is_carried:
                    # The reader asks for a root on a finding with residual_of and on a
                    # located nits_carried entry; any other carried entry (blocks_carried,
                    # hardened_carried, an unlocated nit) links through its own id's root.
                    rootless = (key in ("blocks_carried", "hardened_carried")
                                or (key == "nits_carried" and entry.get("line") is None))
                    _shape(prior is not None and (rootless or entry.get("residual_root") is not None),
                           "carried root")
                held = merged.get(ident)
                if held is None:
                    merged[ident] = [listed_cell, is_carried]
                    continue
                if held[0] == "block" and listed_cell == "block":
                    # One entry for two open BUGs would leave open_bugs below the document's.
                    refuse("receipt_finding_duplicate", ident)
                if SEVERITY[listed_cell] > SEVERITY[held[0]]:
                    held[0] = listed_cell
                held[1] = held[1] or is_carried
        cells = doc.get("cells")
        _shape(type(cells) is dict and set(cells) == set(CELLS)
               and all(type(v) is int and v >= 0 for v in cells.values()), "cells")
        _shape(type(doc.get("open_bugs")) is int and doc["open_bugs"] >= 0, "open_bugs")
        # The reader's rule: block is the own block findings plus every carried
        # block; harden and nit count own findings only; open_bugs is block.
        expected = dict(own, block=own["block"] + len(doc.get("blocks_carried", [])))
        if cells != expected or doc["open_bugs"] != expected["block"]:
            refuse("receipt_document_inconsistent", "cells or open_bugs disagree with the entries")
        findings = [{"id": ident, "cell": cell} for ident, (cell, _) in merged.items()]
        if sum(finding["cell"] == "block" for finding in findings) != doc["open_bugs"]:
            refuse("receipt_document_inconsistent", "the event's block entries disagree with open_bugs")
        carried = [position for position, (_, is_carried) in enumerate(merged.values()) if is_carried]
        fields.update(findings=findings, open_bugs=doc["open_bugs"])
    else:
        _shape(doc.get("open_bugs") is None and not doc.get("findings"), "failed verdict counts")
    return DerivedReceipt(doc, fields, tuple(carried), prior, source, adjudicated)
