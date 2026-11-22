"""Import only indexed document bytes after checking the whole pull request."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from types import SimpleNamespace

from .github import import_base, read_state
from .grammar import parse_import_args
from .ledger_runtime import ledger_module
from .receipt_document import DIGEST, derive_document, parse_document
from .receipt_writer import admitted_writer, append_receipt, prepare_receipt
from .refusal import Refusal, refuse


def _read_file(root, relative):
    parts = relative.split("/")
    if any(p in ("", ".", "..") for p in parts):
        refuse("import_file_unreadable", "index path is not store-relative")
    directory = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            found = os.fstat(handle.fileno())
            if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
                refuse("import_file_unreadable", "index entries must be regular single-link files")
            return handle.read()
    except OSError:
        refuse("import_file_unreadable", "an index entry cannot be opened without following links")
    finally:
        os.close(directory)


def _documents(directory, pr, store_check):
    root = store_check.check_root(directory)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    selected, outcomes, paths = [], [], set()
    try:
        try:
            index = _read_file(descriptor, "review-run.index").decode("utf-8")
        except UnicodeError:
            refuse("import_index_invalid", "index is not UTF-8")
        for line in index.splitlines():
            columns = line.split("\t")
            if len(columns) != 7 or DIGEST.fullmatch(columns[0]) is None:
                refuse("import_index_invalid", "expected seven tab-separated columns")
            digest, path, head, lane, round_, number, patch = columns
            if path in paths:
                refuse("import_index_invalid", "a path is indexed more than once")
            paths.add(path)
            data = _read_file(descriptor, path)
            if hashlib.sha256(data).hexdigest() != digest:
                refuse("import_index_digest_mismatch", "a file differs from its index line")
            # Parse metadata first: documents for other pull requests are not imported.
            doc = parse_document(data)
            if doc.get("pr") is None and doc.get("design_row"):
                outcomes.append("import_design_pass_skipped")
                continue
            if doc.get("pr_repo") is None:
                refuse("receipt_repository_missing", "an indexed document names no repository")
            if type(doc.get("pr_repo")) is not str or type(doc.get("pr")) is not int:
                refuse("receipt_document_shape", "pull request identity")
            if str(doc["pr"]) != number:
                refuse("import_index_identity_mismatch", "index number differs from document")
            if (doc["pr_repo"].casefold(), doc["pr"]) != (pr.key, pr.number):
                continue
            derived = derive_document(data)
            if (head, lane, round_) != (derived.fields["head"], derived.fields["lane"], str(derived.fields["round"])):
                refuse("import_index_identity_mismatch", "index slot differs from document")
            selected.append((digest, data, derived))
    finally:
        os.close(descriptor)
    # Topological dependencies, with deterministic lane/round ordering among ready entries.
    ordered, pending = [], sorted(selected, key=lambda row: (row[2].fields["lane"], row[2].fields["round"], row[0]))
    available = set()
    indexed = {row[0] for row in pending}
    while pending:
        for position, row in enumerate(pending):
            dependencies = {d for d in (row[2].prior, row[2].source) if d in indexed}
            if dependencies <= available:
                ordered.append(row)
                available.add(row[0])
                pending.pop(position)
                break
        else:
            refuse("import_dependency_invalid", "document dependencies do not form an ordered history")
    return ordered, outcomes


def _recorded(events, pr):
    """This pull request's receipts on the item, by slot (lane, round, head) and document digest."""
    return {(e.payload["lane"], e.payload["round"], e.payload["head"], e.payload["doc_sha256"])
            for e in events if (e.payload["repo"], e.payload["pr"]) == (pr.key, pr.number)}


def _slot(row):
    digest, _, derived = row
    return (derived.fields["lane"], derived.fields["round"], derived.fields["head"], digest)


def run(argv, *, records, gh, store_check, out, err):
    try:
        args = parse_import_args(argv)
        documents, outcomes = _documents(args.store, args.pr, store_check)
        with admitted_writer(records, store_check) as lg:
            item = ledger_module("receipts").pr_item(lg, args.pr.url)
            state = read_state(gh, args.pr)
            if (state["repository"].casefold(), state["number"]) != (args.pr.key, args.pr.number):
                refuse("import_base_unresolved", "GitHub answered for another pull request")
            events = lg.events(item_id=item, kind="review.receipt")
            expected = {row[0] for row in documents}
            # A digest already recorded for its slot is that document's receipt, whatever its
            # provenance or recorded base: it is skipped, never written again or compared.
            recorded = _recorded(events, args.pr)
            previous = {slot[3] for slot in recorded}
            pending = [row for row in documents if _slot(row) not in recorded]
            bases = {}
            for _, _, derived in pending:
                head = derived.fields["head"]
                if head not in bases:
                    bases[head] = import_base(gh, args.pr, head, state["baseRefName"])
            projected = list(events)
            for digest, data, derived in pending:
                fields = prepare_receipt(derived, data, projected, base=bases[derived.fields["head"]],
                                         imported_from={"store": args.store, "sha256": digest})
                if ledger_module("credscan").scan(json.dumps(fields).encode()):
                    refuse("receipt_credential_shape", "an imported field holds a credential shape")
                synthetic = dict(fields, doc_sha256=digest)
                projected.append(SimpleNamespace(id=max((e.id for e in projected), default=0) + 1, payload=synthetic))
            written = 0
            try:
                for digest, data, derived in pending:
                    events = lg.events(item_id=item, kind="review.receipt")
                    fields = prepare_receipt(derived, data, events, base=bases[derived.fields["head"]],
                                             imported_from={"store": args.store, "sha256": digest})
                    append_receipt(lg, item, fields, data)
                    written += 1
                readback = {slot[3] for slot in _recorded(lg.events(item_id=item, kind="review.receipt"), args.pr)}
                # Receipts recorded before this run (live or another store's) stay; the run adds the index's.
                if readback != previous | expected:
                    refuse("import_readback_mismatch",
                           "recorded receipts differ from those recorded before plus the index's")
            except Exception:
                if written:
                    refuse("import_partial", "some receipts were recorded; rerun the unchanged store")
                raise
        for outcome in outcomes:
            out.write(outcome + "\n")
        out.write(f"imported {args.pr.url} receipts={len(documents)} written={written}\n")
        return 0
    except Refusal as exc:
        err.write(exc.line() + "\n")
        return 2
    except Exception as exc:
        err.write(f"import_failed: {type(exc).__name__}\n")
        return 2
