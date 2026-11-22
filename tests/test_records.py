"""The records port's seams: grant ids, the write-ahead key and the grant answer's shape."""
from __future__ import annotations

import pytest

from review_gate.records import (
    GrantProof,
    ask_grant,
    divergence_grant_id,
    exception_grant_id,
    merge_key,
    no_ci_grant_id,
    remediation_grant_id,
    review_round_grant_id,
    waiver_grant_id,
)
from review_gate.refusal import Refusal

from rig import LedgerRefusal, StandingGrant, oid

HEAD = oid("head")


def test_every_id_is_built_by_one_constructor_with_the_repository_casefolded():
    assert no_ci_grant_id("Octo/Widget") == "grant:no-ci:octo/widget"
    assert remediation_grant_id("Octo/Widget", 12, HEAD) == f"grant:remediation:octo/widget#12:{HEAD}"
    assert exception_grant_id("Octo/Widget", 12, HEAD) == f"grant:exception:octo/widget#12:{HEAD}"
    assert review_round_grant_id("Octo/Widget", 12, 4) == "grant:review-round:octo/widget#12:r4"
    assert divergence_grant_id("Octo/Widget", 12, 2) == "grant:divergence:octo/widget#12:r2"
    assert waiver_grant_id("Octo/Widget", 12, "src/app.py:40#root1") == "grant:waiver:octo/widget#12:src/app.py:40#root1"
    assert merge_key("Octo/Widget", 12, HEAD) == f"merge:octo/widget#12:{HEAD}"


@pytest.mark.parametrize("call", [
    lambda: no_ci_grant_id("octo"),
    lambda: remediation_grant_id("octo/widget", 0, HEAD),
    lambda: remediation_grant_id("octo/widget", 12, HEAD.upper()),
    lambda: remediation_grant_id("octo/widget", True, HEAD),
    lambda: review_round_grant_id("octo/widget", 12, 0),
    lambda: waiver_grant_id("octo/widget", 12, "no line"),
    lambda: merge_key("octo/widget", 12, HEAD[:12]),
])
def test_a_malformed_part_raises(call):
    with pytest.raises(ValueError):
        call()


class Port:
    def __init__(self, answer):
        self.answer = answer

    def standing(self, grant_id):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


GID = "grant:no-ci:octo/widget"


def test_a_standing_answer_is_kept_as_its_id_and_deciding_event():
    assert ask_grant(Port(StandingGrant(GID, "STANDING", 9)), GID, "unchecked_head", "ctx") == GrantProof(GID, 9)


@pytest.mark.parametrize("code", ["grant_unknown", "grant_withdrawn", "grant_unverified"])
def test_each_ledger_code_refuses_by_the_stages_name(code):
    with pytest.raises(Refusal) as refused:
        ask_grant(Port(LedgerRefusal("no", code)), GID, "unchecked_head", "ctx")
    assert refused.value.name == "unchecked_head"
    assert code in refused.value.detail


@pytest.mark.parametrize("answer", [
    True,
    "STANDING",
    "WITHDRAWN",
    StandingGrant(GID, "WITHDRAWN", 9),
    StandingGrant("grant:no-ci:octo/other", "STANDING", 9),
    StandingGrant(GID, "STANDING", 0),
    StandingGrant(GID, "STANDING", True),
    None,
])
def test_a_truthy_or_wrong_answer_is_no_grant(answer):
    with pytest.raises(Refusal) as refused:
        ask_grant(Port(answer), GID, "unchecked_head", "ctx")
    assert refused.value.name == "grant_proof_invalid"


def test_any_other_error_refuses_grant_lookup_failed():
    with pytest.raises(Refusal) as refused:
        ask_grant(Port(RuntimeError("db gone")), GID, "unchecked_head", "ctx")
    assert refused.value.name == "grant_lookup_failed"


def test_an_unknown_code_is_not_mapped_to_the_stage():
    with pytest.raises(Refusal) as refused:
        ask_grant(Port(LedgerRefusal("no", "db_locked")), GID, "unchecked_head", "ctx")
    assert refused.value.name == "grant_lookup_failed"


def test_a_refusal_from_the_port_passes_as_it_is():
    with pytest.raises(Refusal) as refused:
        ask_grant(Port(Refusal("records_unavailable", "x")), GID, "unchecked_head", "ctx")
    assert refused.value.name == "records_unavailable"
