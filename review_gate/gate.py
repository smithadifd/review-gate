"""The merge command's stage sequencer.

Stages run in ``STAGE_ORDER``, every one of them, and any refusal stops the
run before the next:

0. arguments (``grammar``), before any read of GitHub, the records port or
   the filesystem;
1. store (``store``): the database the records port names is not where a
   lane can write;
2. state read 1 and the file list (``github``);
3. item, 4. receipts, 5. open bugs, 6. hardening, 7. round cap: calls on the
   records port, each answering a token;
8. CI at job grain (``ci``);
9. main is green (``main_green``);
10. recheck: the file list and the state read again, equal to the first on
    every field; stages 3 to 7 and every grant used asked again, each
    answer equal to the first; then the base is in the head, compared
    against the tip stage 9 judged;
11. write-ahead and the merge (``merge``).

Output: exit 0 with one stdout line on a confirmed, recorded merge; exit 2
with one stderr line starting with the refusal's name when nothing was
merged; exit 4 when a merge call was made and its outcome is not confirmed.
Any error that escapes a stage is reported as a named refusal
(``internal_error``), never a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ci import stage_ci
from .github import compare_state, files_digest, read_files, read_state, state_gate, base_in_head
from .grammar import ITEM_ID, MergeArgs, parse_merge_args
from .main_green import stage_main_green
from .merge import UNCONFIRMED, stage_merge
from .records import GrantProof, Subject, ask_grant, port_call
from .refusal import Refusal, refuse

REFUSED = 2
RECORDS_STAGES = ("receipts", "open_bugs", "hardening", "round_cap")


@dataclass
class Gate:
    args: MergeArgs
    records: object
    gh: object
    store_check: object
    out: object
    err: object
    stage: str = "arguments"
    store: str | None = None
    state: dict | None = None
    paths: list | None = None
    digest: str | None = None
    subject: Subject | None = None
    item: str | None = None
    tokens: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    no_ci_grant: GrantProof | None = None
    remediation_grant: GrantProof | None = None
    tip: str | None = None
    merge_called: bool = False


def stage_store(gate: Gate) -> None:
    paths = port_call(gate.records, "database_paths")
    gate.store = gate.store_check.check(paths)


def stage_state_read(gate: Gate) -> None:
    args = gate.args
    gate.state = read_state(gate.gh, args.pr)
    state_gate(gate.state, args.pr, args.sha)
    gate.paths = read_files(gate.gh, args.pr, gate.state)
    gate.digest = files_digest(gate.paths)
    gate.subject = Subject(
        url=args.url,
        repo=args.pr.key,
        number=args.pr.number,
        head=args.sha,
        base=gate.state["default"],
        files_sha256=gate.digest,
        items=args.items,
    )


def _item(gate: Gate) -> str:
    item = port_call(gate.records, "item", gate.subject)
    if not isinstance(item, str) or ITEM_ID.fullmatch(item) is None:
        refuse("records_answer_invalid", f"the records port's item for {gate.args.url} is {item!r}")
    return item


def _token(gate: Gate, stage: str):
    token = port_call(gate.records, stage, gate.subject, gate.item)
    if token is None:
        refuse("records_answer_invalid", f"the records port answered nothing for stage {stage}")
    return token


def stage_item(gate: Gate) -> None:
    gate.item = _item(gate)


def _records_stage(stage: str):
    def run(gate: Gate) -> None:
        gate.tokens[stage] = _token(gate, stage)

    run.__name__ = f"stage_{stage}"
    return run


def _same_grant(gate: Gate, proof: GrantProof | None, refusal: str) -> None:
    if proof is None:
        return
    again = ask_grant(gate.records, proof.grant_id, refusal, "recheck")
    if again != proof:
        refuse(
            "grant_moved_before_merge",
            f"{proof.grant_id} was decided by event {proof.deciding_event_id}, now {again.deciding_event_id}",
        )


def stage_recheck(gate: Gate) -> None:
    args = gate.args
    paths = read_files(gate.gh, args.pr, gate.state)
    final = read_state(gate.gh, args.pr)
    compare_state(gate.state, final)
    state_gate(final, args.pr, args.sha)
    digest = files_digest(paths)
    if digest != gate.digest:
        refuse("pr_files_moved_before_merge", f"files_sha256 {gate.digest} -> {digest}")
    item = _item(gate)
    if item != gate.item:
        refuse("records_moved_before_merge", f"item {gate.item} -> {item}")
    for stage in RECORDS_STAGES:
        if _token(gate, stage) != gate.tokens[stage]:
            refuse("records_moved_before_merge", f"the records port's answer for stage {stage} changed")
    _same_grant(gate, gate.no_ci_grant, "unchecked_head")
    _same_grant(gate, gate.remediation_grant, "remediation_not_granted")
    tip = base_in_head(gate.gh, args.pr, args.sha, final["default"])
    if tip != gate.tip:
        refuse(
            "main_moved_during_gate",
            f"stage 9 judged the default branch at {gate.tip}; the base check read it at {tip}",
        )


# The stage order, fixed: (name, function). Stage 0 runs before this list.
STAGES = (
    ("store", stage_store),
    ("state_read", stage_state_read),
    ("item", stage_item),
    *((stage, _records_stage(stage)) for stage in RECORDS_STAGES),
    ("ci", stage_ci),
    ("main_green", stage_main_green),
    ("recheck", stage_recheck),
    ("merge", stage_merge),
)
STAGE_ORDER = ("arguments",) + tuple(name for name, _ in STAGES)


def run(argv, *, records, gh, store_check, out, err) -> int:
    """Run ``review-gate`` with ``argv`` (program name excluded); return the exit code."""
    gate = None
    try:
        args = parse_merge_args(argv)
        gate = Gate(args=args, records=records, gh=gh, store_check=store_check, out=out, err=err)
        result = None
        for name, stage in STAGES:
            gate.stage = name
            result = stage(gate)
        return result
    except Refusal as refusal:
        if gate is not None and gate.merge_called:
            err.write(f"merge_unconfirmed: {refusal.line()}\n")
            return UNCONFIRMED
        err.write(refusal.line() + "\n")
        return REFUSED
    except Exception as exc:  # never a traceback
        stage = gate.stage if gate is not None else "arguments"
        detail = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
        if gate is not None and gate.merge_called:
            err.write(f"merge_unconfirmed: internal error at stage {stage}: {detail}\n")
            return UNCONFIRMED
        err.write(f"internal_error: stage {stage}: {detail}\n")
        return REFUSED
