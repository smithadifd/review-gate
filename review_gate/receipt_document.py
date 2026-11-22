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
SOURCES = ("settled_from", "adjudicated_from", "reparsed_from")


def parse_document(data):
    if type(data) is not bytes:
        refuse("receipt_doc_not_json", "the document must be bytes")
    if len(data) > 65536:
        refuse("receipt_doc_too_large", "the inline limit is 65536 bytes")

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


@dataclass(frozen=True)
class DerivedReceipt:
    document: dict
    fields: dict
    carried: tuple
    prior: str | None
    source: str | None


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
    sources = [key for key in SOURCES if key in doc]
    _shape(len(sources) <= 1, "one sibling source")
    source = document_digest(doc[sources[0]], sources[0]) if sources else None
    carried = []
    if verdict in ("approved", "changes_requested"):
        _shape(model is not None and doc.get("severity_rubric") == "v2", "counted document")
        entries = []
        for key in ("findings", "hardened_carried"):
            _shape(type(doc.get(key)) is list, key)
        for key in ("blocks_carried", "nits_carried", "resolved_carried", "standing_carried", "unsettled_carried"):
            _shape(key not in doc or type(doc[key]) is list, key)
        resolved = doc.get("resolved_carried", [])
        for key in ("findings", "hardened_carried", "blocks_carried", "nits_carried"):
            for entry in doc.get(key, []):
                _shape(type(entry) is dict, key)
                if key == "hardened_carried" and any(
                    all(entry.get(k) == fixed.get(k) for k in ("file", "line", "description"))
                    for fixed in resolved if type(fixed) is dict):
                    continue
                entries.append((entry, key != "findings" or entry.get("residual_of") is not None))
        findings, seen = [], set()
        counts = dict.fromkeys(CELLS, 0)
        for entry, is_carried in entries:
            cell = entry.get("cell")
            if cell not in CELLS:
                refuse("receipt_cell_unknown", "a finding has no known cell")
            ident = finding_id(entry)
            if ident in seen:
                refuse("receipt_finding_duplicate", ident)
            seen.add(ident)
            counts[cell] += 1
            findings.append({"id": ident, "cell": cell})
            if is_carried:
                _shape(prior is not None and entry.get("residual_root") is not None, "carried root")
                carried.append(len(findings) - 1)
        cells = doc.get("cells")
        _shape(type(cells) is dict and set(cells) == set(CELLS)
               and all(type(v) is int and v >= 0 for v in cells.values()), "cells")
        _shape(type(doc.get("open_bugs")) is int and doc["open_bugs"] >= 0, "open_bugs")
        if cells != counts or doc["open_bugs"] != counts["block"]:
            refuse("receipt_document_inconsistent", "cells or open_bugs disagree with the entries")
        fields.update(findings=findings, open_bugs=counts["block"])
    else:
        _shape(doc.get("open_bugs") is None and not doc.get("findings"), "failed verdict counts")
    return DerivedReceipt(doc, fields, tuple(carried), prior, source)
