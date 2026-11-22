"""Recover records after a merge; this command has no merge call.

Grammar, store admission, current PR-item lookup, then a live merged read at
exactly the supplied head. Started writes done before closing; finished only
closes. Absent refuses. A failed close is retriable without repeating a merge.
"""
from __future__ import annotations

from .github import merged_state
from .grammar import parse_recovery_args
from .merge import _merged_at
from .records import MERGE_STATES, merge_key, port_call
from .refusal import Refusal, refuse


def run(argv, *, records, gh, store_check, out, err):
    try:
        args = parse_recovery_args(argv)
        store_check.check(port_call(records, "database_paths"))
        # Item lookup consumes only url/items; recovery does not judge receipts.
        item = port_call(records, "item", args)
        read = merged_state(gh, args.pr)
        commit = _merged_at(read, args.sha)
        if commit is None:
            refuse("merge_intent_unreconciled", "GitHub does not read MERGED at the supplied head")
        key = merge_key(args.pr.slug, args.pr.number, args.sha)
        state = port_call(records, "merge_state", item, key)
        if state not in MERGE_STATES:
            refuse("records_answer_invalid", "unknown write-ahead state")
        if state == "absent":
            refuse("merge_intent_missing", key)
        if state == "started":
            port_call(records, "write_finished", item, key, commit)
        port_call(records, "close_items", args.items, args.url, args.sha)
        out.write(f"recovered {args.url} head={args.sha} merge_commit={commit}\n")
        return 0
    except Refusal as exc:
        err.write(exc.line() + "\n")
        return 2
    except Exception as exc:
        err.write(f"recovery_failed: {type(exc).__name__}\n")
        return 2
