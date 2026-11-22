"""Write document-derived receipts on an existing pull request item."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os

from .ledger_runtime import ledger_module
from .receipt_document import COMMIT, derive_document
from .records import Subject
from .refusal import refuse


@contextmanager
def admitted_writer(records, store_check):
    """Admission precedes every open; concurrent review-gate writers serialize."""
    paths = records.database_paths()
    store_check.check(paths)
    descriptor = os.open(os.path.dirname(paths.database),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with records._open(write=True) as lg:
            yield lg
    finally:
        os.close(descriptor)


def prepare_receipt(derived, data, events, *, base, imported_from=None):
    """Resolve only item-local document digests; never take event ids from JSON."""
    fields = dict(derived.fields)
    fields["findings"] = [dict(f) for f in fields.get("findings", [])]
    if derived.fields["verdict"] not in ("approved", "changes_requested"):
        fields.pop("findings")
    if type(base) is not str or COMMIT.fullmatch(base) is None:
        refuse("receipt_document_shape", "base")
    fields["base"] = base
    if imported_from is not None:
        fields["imported_from"] = imported_from
    digest = hashlib.sha256(data).hexdigest()
    relevant = [e for e in events if (e.payload["repo"], e.payload["pr"])
                == (fields["repo"], fields["pr"])]
    same = [e for e in relevant if (e.payload["lane"], e.payload["round"], e.payload["head"])
            == (fields["lane"], fields["round"], fields["head"])]
    retry = next((e for e in same if e.payload["doc_sha256"] == digest), None)
    history = [e for e in relevant if retry is None or e.id < retry.id]

    def by_digest(wanted, name):
        found = [e for e in history if e.payload["doc_sha256"] == wanted]
        if len(found) != 1:
            refuse(name, "document digest must resolve exactly once on this item")
        return found[0]

    if derived.prior is not None:
        prior = by_digest(derived.prior, "receipt_prior_mismatch")
        if (prior.payload["lane"], prior.payload["round"]) != (fields["lane"], fields["round"] - 1):
            refuse("receipt_prior_mismatch", "prior document is not the lane's previous round")
        fields["prior_receipt"] = prior.id
        for position in derived.carried:
            finding = fields["findings"][position]
            root = finding["id"].rsplit("#", 1)[1]
            matches = [f for f in prior.payload.get("findings", []) if f["id"].rsplit("#", 1)[1] == root]
            if len(matches) != 1:
                refuse("receipt_carried_root_missing", finding["id"])
            finding["carried_from"] = prior.id
    if derived.source is not None:
        source = by_digest(derived.source, "receipt_supersedes_mismatch")
        older = [e for e in same if retry is None or e.id < retry.id]
        if not older or source.id != max(e.id for e in older):
            refuse("receipt_supersedes_mismatch", "source is not the newest document in this slot")
        fields["supersedes"] = source.id
    if (fields["round"] == 1 and retry is None and not same and derived.source is None
            and any(e.payload["lane"] == fields["lane"] for e in relevant)):
        refuse("receipt_round_restart", "this lane already has a receipt for this pull request")
    if same and retry is None and derived.source is None:
        refuse("receipt_supersedes_required", "another document already records this slot")
    schema = ledger_module("schema")
    try:
        schema.validate_payload("review.receipt", dict(fields, doc_sha256=digest))
    except ledger_module("schema").ValidationError:
        refuse("receipt_document_shape", "derived fields do not fit the receipt schema")
    if ledger_module("credscan").scan(data):
        refuse("doc_credential_shape", "the document holds a credential shape; text is not shown")
    return fields


def append_receipt(lg, item, fields, data):
    try:
        return ledger_module("receipts").write_receipt(lg, item, fields, data, harness="review-gate")
    except ledger_module("store").IdempotencyConflict:
        refuse("receipt_idempotency_conflict", "an existing receipt has different fields or attribution")


def write_receipt(document, *, records, store_check):
    """Take bytes, never a path or caller-supplied decided fields."""
    derived = derive_document(document)
    with admitted_writer(records, store_check) as lg:
        p = derived.fields
        url = f"https://github.com/{p['repo']}/pull/{p['pr']}"
        item = ledger_module("receipts").pr_item(lg, url)
        events = lg.events(item_id=item, kind="review.receipt")
        fields = prepare_receipt(derived, document, events, base=derived.document.get("base"))
        subject = Subject(url, p["repo"], p["pr"], p["head"], "", p["changed_files_sha256"], (item,))
        for finding in fields.get("findings", []):
            if "carried_from" in finding:
                records._root_finding(lg, subject, item, None, finding)
        return append_receipt(lg, item, fields, document)
