"""The records port: every ledger-facing call the merge command makes.

One object carries every call (``RecordsPort``). The merge command never
opens the ledger itself; it asks the port, and the port answers from the
ledger database. LEDGER_DB selects the filled ``LedgerRecords`` port and
names only an absolute database file. Unset or empty selects
``UnfilledRecords`` and refuses ``records_unavailable``. Neither arguments
nor configuration can change the port class, stage list or policy.

The calls, by stage:

- stage 1: ``database_paths()`` names the database file and its documents
  folder, as the ledger resolves them; stage 1 checks exactly those paths.
- stages 3 to 7: ``item(subject)`` returns the item id the pull request's
  ``pr`` link resolves to; ``receipts``, ``open_bugs``, ``hardening`` and
  ``round_cap`` each take the subject and that item and return a token that
  stands for the answer, or raise a ``Refusal``. Stage 10 asks every one of
  them again and refuses unless each token is equal to the first one.
- grants: ``standing(grant_id)`` mirrors the ledger's
  ``ledger.verbs.grants.standing``: it returns the grant only when it is
  STANDING and raises otherwise, with a ``code`` of ``grant_unknown``,
  ``grant_withdrawn`` or ``grant_unverified``. There is no boolean or
  status-string answer, because a status string such as ``"WITHDRAWN"`` is
  truthy.
- stage 11: ``merge_state(item, key)`` returns ``absent``, ``started`` or
  ``finished`` for the write-ahead key; ``write_intent`` and
  ``write_finished`` record the two transitions; ``close_items`` closes the
  ``--items`` after a confirmed merge.

Grant ids and the write-ahead key are built only by the constructors below,
so every reader and every writer of an ask uses one spelling. The
``owner/repo`` part is casefolded first: the ledger casefolds pull request
targets, and grant ids compare as exact strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .refusal import Refusal, refuse

STANDING = "STANDING"
# The codes ledger.verbs.grants.standing raises for a grant that is not standing.
GRANT_CODES = ("grant_unknown", "grant_withdrawn", "grant_unverified")
MERGE_STATES = ("absent", "started", "finished")
# Every call of the port, in the order a full merge makes them.
RECORDS_METHODS = (
    "database_paths",
    "item",
    "receipts",
    "open_bugs",
    "hardening",
    "round_cap",
    "standing",
    "merge_state",
    "write_intent",
    "write_finished",
    "close_items",
)
_SHA = re.compile(r"[0-9a-f]{40}")
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_FINDING = re.compile(r"[^\s:#]+:[1-9][0-9]*#\S+")


@dataclass(frozen=True)
class DatabasePaths:
    """The ledger database file and its documents folder, absolute paths."""

    database: str
    documents: str


@dataclass(frozen=True)
class Subject:
    """What stages 3 to 7 decide about: one pull request at one head and file list."""

    url: str
    repo: str  # owner/repo, casefolded
    number: int
    head: str
    base: str
    files_sha256: str
    items: tuple


class RecordsPort(Protocol):
    def database_paths(self) -> DatabasePaths: ...

    def item(self, subject: Subject) -> str: ...

    def receipts(self, subject: Subject, item: str) -> Any: ...

    def open_bugs(self, subject: Subject, item: str) -> Any: ...

    def hardening(self, subject: Subject, item: str) -> Any: ...

    def round_cap(self, subject: Subject, item: str) -> Any: ...

    def standing(self, grant_id: str) -> Any: ...

    def merge_state(self, item: str, key: str) -> str: ...

    def write_intent(self, item: str, key: str) -> None: ...

    def write_finished(self, item: str, key: str, merge_commit: str) -> None: ...

    def close_items(self, items: tuple, url: str, head: str) -> None: ...


UNFILLED_DETAIL = (
    "LEDGER_DB is unset or empty; name an absolute ledger database file"
)


class UnfilledRecords:
    """The no-database port: every call refuses ``records_unavailable``."""

    def _refuse(self, *_args, **_kwargs):
        refuse("records_unavailable", UNFILLED_DETAIL)

    database_paths = _refuse
    item = _refuse
    receipts = _refuse
    open_bugs = _refuse
    hardening = _refuse
    round_cap = _refuse
    standing = _refuse
    merge_state = _refuse
    write_intent = _refuse
    write_finished = _refuse
    close_items = _refuse


# ---- grant ids and the write-ahead key (one constructor per kind) --------

def _repo(repo: str) -> str:
    if not isinstance(repo, str) or _SLUG.fullmatch(repo) is None:
        raise ValueError(f"not an owner/repo: {repo!r}")
    return repo.casefold()


def _number(number: int) -> int:
    if type(number) is not int or number < 1:
        raise ValueError(f"not a pull request number: {number!r}")
    return number


def _head(head: str) -> str:
    if not isinstance(head, str) or _SHA.fullmatch(head) is None:
        raise ValueError(f"not a full head sha: {head!r}")
    return head


def _round(round_: int) -> int:
    if type(round_) is not int or round_ < 1:
        raise ValueError(f"not a round: {round_!r}")
    return round_


def no_ci_grant_id(repo: str) -> str:
    """The standing grant that lifts the post-READY CI requirement for a repository with no workflow file."""
    return f"grant:no-ci:{_repo(repo)}"


def remediation_grant_id(repo: str, number: int, head: str) -> str:
    """The grant to merge onto a red main, for one pull request at one head."""
    return f"grant:remediation:{_repo(repo)}#{_number(number)}:{_head(head)}"


def exception_grant_id(repo: str, number: int, head: str) -> str:
    """The single-family exception on a code pull request, for one head."""
    return f"grant:exception:{_repo(repo)}#{_number(number)}:{_head(head)}"


def review_round_grant_id(repo: str, number: int, round_: int) -> str:
    """A review round past the cap, for one pull request."""
    return f"grant:review-round:{_repo(repo)}#{_number(number)}:r{_round(round_)}"


def divergence_grant_id(repo: str, number: int, round_: int) -> str:
    """The divergence waiver at one round of one pull request."""
    return f"grant:divergence:{_repo(repo)}#{_number(number)}:r{_round(round_)}"


def waiver_grant_id(repo: str, number: int, finding: str) -> str:
    """The waiver of one open finding (``<path>:<line>#<root>``) on one pull request."""
    if not isinstance(finding, str) or _FINDING.fullmatch(finding) is None:
        raise ValueError(f"not a finding id: {finding!r}")
    return f"grant:waiver:{_repo(repo)}#{_number(number)}:{finding}"


def merge_key(repo: str, number: int, head: str) -> str:
    """The write-ahead key of one merge: ``merge:<owner>/<repo>#<N>:<head>``."""
    return f"merge:{_repo(repo)}#{_number(number)}:{_head(head)}"


# ---- asking the port -----------------------------------------------------

def port_call(records, method: str, *args):
    """One call on the records port; a ``Refusal`` passes as it is, any other error refuses ``records_read_failed``."""
    try:
        return getattr(records, method)(*args)
    except Refusal:
        raise
    except Exception as exc:
        refuse("records_read_failed", f"{method}: {type(exc).__name__}")


@dataclass(frozen=True)
class GrantProof:
    """What the gate keeps of a standing grant: its id and the election that decided it."""

    grant_id: str
    deciding_event_id: int


def ask_grant(records, grant_id: str, refusal: str, context: str) -> GrantProof:
    """The proof that ``grant_id`` is STANDING, or a refusal named ``refusal``.

    The port's answer must be an object whose ``grant_id`` is exactly
    ``grant_id``, whose ``status`` is exactly ``STANDING`` and whose
    ``deciding_event_id`` is a positive integer; anything else refuses
    ``grant_proof_invalid``. A raised error with a ``code`` in
    ``GRANT_CODES`` refuses ``refusal``; a ``Refusal`` (such as the
    unfilled port's) is raised as it is; any other error refuses
    ``grant_lookup_failed``.
    """
    try:
        answer = records.standing(grant_id)
    except Refusal:
        raise
    except Exception as exc:  # the port's refusal, mapped by its code
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code in GRANT_CODES:
            refuse(refusal, f"{context}: {grant_id} is not standing ({code})")
        refuse("grant_lookup_failed", f"{grant_id}: {type(exc).__name__}")
    if getattr(answer, "grant_id", None) != grant_id or getattr(answer, "status", None) != STANDING:
        refuse("grant_proof_invalid", f"the records port's answer for {grant_id} is not a standing grant")
    event = getattr(answer, "deciding_event_id", None)
    if type(event) is not int or event < 1:
        refuse("grant_proof_invalid", f"the records port's answer for {grant_id} names no deciding event")
    return GrantProof(grant_id=grant_id, deciding_event_id=event)
