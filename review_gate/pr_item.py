"""Explicit item creation; receipt writers remain lookup-only.

Creation and linking commit together, or neither does. BEGIN IMMEDIATE also
serializes ledger CLI and loop writers that do not take admitted_writer's
lock. A stopped transaction rolls back; a stop after commit is a no-op on
retry. Raw ledger writers can still introduce ambiguity after this commits.
"""
from __future__ import annotations

import hashlib

from .grammar import parse_item_args
from .ledger_runtime import ledger_module
from .receipt_writer import admitted_writer
from .refusal import Refusal, refuse


def _lookup(lg, url):
    try:
        return ledger_module("receipts").pr_item(lg, url)
    except ledger_module("verbs").CliError as exc:
        if exc.code == "pr_item_missing":
            return None
        raise


def _create(lg, pr):
    url = f"https://github.com/{pr.key}/pull/{pr.number}"
    source = "pr:" + url
    key = "item.created:" + source
    item = "it-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    payload = {"type": "ticket", "title": url, "source": source}
    prior = lg.event_by_key(key)
    if prior is not None:
        if prior.kind != "item.created" or prior.item_id != item or prior.payload != payload:
            refuse("pr_item_source_conflict", "source already names a different creation")
    elif lg.item(item) is not None:
        refuse("pr_item_source_conflict", "derived item id already exists without this source")

    def unlinked(conn):
        # pr_item reads lg's connection, inside this append's transaction.
        linked = _lookup(lg, url)
        if linked is not None and linked != item:
            refuse("pr_item_link_conflict", "another item now links this pull request")
        if lg.events(item_id=item, kind="loop.managed"):
            refuse("pr_item_loop_managed", "the loop owns this item's pull request link")
        if any(e.payload.get("rel") == "pr" for e in lg.events(item_id=item, kind="item.linked")):
            refuse("pr_item_foreign_link", "the source item already has a foreign pull request link")

    # Validate before creation too; the append repeats the cross-item check
    # at the write boundary, never relying on a previously returned item id.
    unlinked(None)
    if prior is None:
        lg.append(kind="item.created", item_id=item, payload=payload,
                  idempotency_key=key, harness="review-gate", expect_revision="none")
    lg.append(kind="item.linked", item_id=item, payload={"rel": "pr", "target": url},
              idempotency_key="review-gate:pr-link:" + url, harness="review-gate",
              precondition=unlinked)
    return item


def run(argv, *, records, gh, store_check, out, err):
    try:
        args = parse_item_args(argv)
        if args.lookup:
            paths = records.database_paths()
            store_check.check(paths)
            with records._open() as lg:
                item = ledger_module("receipts").pr_item(lg, args.pr.url)
        else:
            with admitted_writer(records, store_check) as lg:
                with lg.transaction():
                    item = _lookup(lg, args.pr.url)
                    if item is None:
                        item = _create(lg, args.pr)
        out.write(item + "\n")
        return 0
    except Refusal as exc:
        err.write(exc.line() + "\n")
        return 3 if exc.name == "pr_item_missing" else 2
    except Exception as exc:
        err.write(f"item_failed: {type(exc).__name__}\n")
        return 2
