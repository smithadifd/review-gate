"""Stage 11: write-ahead, then the head-pinned merge call, then the finished record.

The write-ahead key is ``merge:<owner>/<repo>#<N>:<head>`` on the item
stage 3 resolved. Its state, read through the records port, is one of:

- ``absent``: the intent is written first; a failed intent write refuses
  and no merge call is made. Then one call,
  ``gh pr merge <URL> --squash --match-head-commit <the argument sha>``.
  Whatever it exits, GitHub is read again: only a read showing the pull
  request MERGED, at the pinned head, with a merge commit, counts. Then the
  finished record is written with that merge commit, and only then are the
  ``--items`` closed. One stdout line, ``merged <URL> head=<sha>
  merge_commit=<oid>``, and exit 0.
- ``started``: an earlier run wrote the intent. GitHub is read and the
  state reconciled: MERGED at the pinned head writes the finished record (no
  merge call); anything else refuses ``merge_intent_unreconciled`` and no
  second merge call is made.
- ``finished``: refuses ``merge_already_finished``.

Exit 4 means a merge call was made and its outcome is not confirmed in the
records (``merge_unconfirmed``, ``merge_finish_unrecorded``); exit 0 means
merged and recorded.
"""
from __future__ import annotations

from .github import OID, merged_state
from .records import MERGE_STATES, merge_key, port_call
from .refusal import Refusal, refuse

MERGED = 0
UNCONFIRMED = 4


def merge_argv(url: str, sha: str) -> list:
    return ["pr", "merge", url, "--squash", "--match-head-commit", sha]


def _merged_at(read: dict, sha: str) -> str | None:
    """The merge commit when the read shows the pull request MERGED at ``sha``, else None."""
    commit = read.get("mergeCommit")
    if (
        read.get("state") == "MERGED"
        and read.get("merged") is True
        and read.get("headRefOid") == sha
        and isinstance(commit, str)
        and OID.fullmatch(commit) is not None
    ):
        return commit
    return None


def _finish(gate, key: str, commit: str) -> int:
    args = gate.args
    try:
        gate.records.write_finished(gate.item, key, commit)
    except Exception as exc:  # the merge stands; the record does not
        detail = exc.line() if isinstance(exc, Refusal) else type(exc).__name__
        gate.err.write(
            f"merge_finish_unrecorded: {args.url} merged at {commit} but the finished record was not written ({detail})\n"
        )
        return UNCONFIRMED
    try:
        gate.records.close_items(args.items, args.url, args.sha)
    except Exception as exc:  # a closure failure never changes the merge's outcome
        detail = exc.line() if isinstance(exc, Refusal) else type(exc).__name__
        gate.err.write(f"items_close_failed: {', '.join(args.items)} ({detail})\n")
    gate.out.write(f"merged {args.url} head={args.sha} merge_commit={commit}\n")
    return MERGED


def stage_merge(gate) -> int:
    args = gate.args
    pr = args.pr
    key = merge_key(pr.slug, pr.number, args.sha)
    state = port_call(gate.records, "merge_state", gate.item, key)
    if state not in MERGE_STATES:
        refuse("records_answer_invalid", f"the write-ahead state of {key} is {state!r}")
    if state == "finished":
        refuse("merge_already_finished", f"{key} is recorded as finished")
    if state == "started":
        commit = _merged_at(merged_state(gate.gh, pr), args.sha)
        if commit is None:
            refuse(
                "merge_intent_unreconciled",
                f"an earlier run wrote the intent {key} and GitHub does not read the pull request MERGED at "
                f"{args.sha}; no second merge call is made",
            )
        return _finish(gate, key, commit)
    try:
        gate.records.write_intent(gate.item, key)
    except Refusal:
        raise
    except Exception as exc:
        refuse("merge_intent_unwritten", f"{key}: {type(exc).__name__}")
    gate.merge_called = True
    result = gate.gh.run(merge_argv(args.url, args.sha))
    try:
        read = merged_state(gate.gh, pr)
    except Refusal as refusal:
        gate.err.write(f"merge_unconfirmed: gh pr merge exited {result.returncode}; {refusal.line()}\n")
        return UNCONFIRMED
    commit = _merged_at(read, args.sha)
    if commit is None:
        gate.err.write(
            f"merge_unconfirmed: gh pr merge exited {result.returncode}; GitHub reads state={read.get('state')!r} "
            f"head={read.get('headRefOid')!r}\n"
        )
        return UNCONFIRMED
    return _finish(gate, key, commit)
