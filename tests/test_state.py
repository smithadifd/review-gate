"""Stages 2 and 10: the state reads, the file list and its digest, the recheck and the base check."""
from __future__ import annotations

import pytest

from review_gate.github import STATE_FIELDS, STATE_QUERY, files_digest

from rig import LedgerRefusal, key, oid


def test_the_state_query_reads_no_body():
    assert "body" not in STATE_QUERY


def test_a_head_that_is_not_the_given_sha_refuses(rig):
    rig.repo.state1 = {"headRefOid": oid("moved")}
    result = rig.run()
    assert result.reason == "head_mismatch", result.err
    assert rig.merge_calls() == []


@pytest.mark.parametrize("override,reason", [
    ({"state": "CLOSED"}, "pr_not_open"),
    ({"state": "MERGED"}, "pr_not_open"),
    ({"isDraft": True}, "pr_is_draft"),
    ({"baseRefName": "develop"}, "pr_base_not_default"),
    ({"isCrossRepository": True}, "pr_head_fork"),
    ({"headRepository": "someone/widget"}, "pr_head_fork"),
    ({"headRepository": None}, "pr_head_fork"),
    ({"nameWithOwner": "octo/other"}, "pr_repository_mismatch"),
    ({"number": 8}, "pr_repository_mismatch"),
    ({"__doc__": {"errors": [{"message": "boom"}]}}, "pr_view_unparseable"),
    ({"__doc__": {"data": {"repository": None}}}, "pr_view_unparseable"),
    ({"__doc__": {"data": {"repository": {"nameWithOwner": "octo/widget", "defaultBranchRef": {"name": "main"},
                                          "pullRequest": None}}}}, "pr_view_unparseable"),
    ({"isDraft": "false"}, "pr_view_unparseable"),
])
def test_state_read_1_refuses_by_name(rig, override, reason):
    rig.repo.state1 = override
    result = rig.run()
    assert result.code == 2
    assert result.reason == reason, result.err
    assert rig.merge_calls() == []


def test_a_failed_state_read_refuses(rig):
    rig.repo.extra = {key(rig.repo.graphql_argv(STATE_QUERY)): [{"stdout": "", "stderr": "HTTP 502\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "pr_view_failed"
    assert rig.merge_calls() == []


def test_the_file_list_at_the_cap_refuses(rig):
    rig.repo.files = [[f"src/f{i}.py", None] for i in range(300)]
    result = rig.run()
    assert result.reason == "compare_files_capped"


def test_a_file_count_unlike_changed_files_refuses(rig):
    rig.repo.state1 = {"changedFiles": 2}
    result = rig.run()
    assert result.reason == "pr_files_count_mismatch"


def test_the_digest_is_the_review_runners():
    """A fixture of known input and known digest: sorted, de-duplicated, each path NUL-terminated."""
    assert files_digest(["b.py", "a.py", "b.py"]) == "3a901b2057022886c7795c50757b3b8930f1886f8ba2198f145ef2336c05d123"


def test_a_rename_counts_both_names_in_the_digest(rig):
    rig.repo.files = [["src/new.py", "src/old.py"]]
    result = rig.run()
    assert result.code == 0, result.err
    assert {subject.files_sha256 for subject in rig.port.subjects} == {
        "4144168ea16d5eea198b6bfc20878af286efe673908fc0295eab9afde5f435c4"
    }
    assert {subject.head for subject in rig.port.subjects} == {rig.repo.head}
    assert {subject.repo for subject in rig.port.subjects} == {"octo/widget"}


# Every field read 2 must equal read 1 on, with its refusal name; written out
# here so a field dropped from the production list still has its case.
MOVED_FIELDS = (
    ("repository", "repo_moved_before_merge"),
    ("default", "repo_default_branch_moved_before_merge"),
    ("number", "pr_number_moved_before_merge"),
    ("headRefOid", "pr_head_moved_before_merge"),
    ("headRefName", "pr_head_branch_moved_before_merge"),
    ("headRepository", "pr_head_repository_moved_before_merge"),
    ("isCrossRepository", "pr_cross_repository_moved_before_merge"),
    ("baseRefName", "pr_base_moved_before_merge"),
    ("baseRefOid", "pr_base_oid_moved_before_merge"),
    ("state", "pr_state_moved_before_merge"),
    ("isDraft", "pr_draft_moved_before_merge"),
    ("changedFiles", "pr_files_moved_before_merge"),
)


def test_the_moved_fields_are_the_production_list():
    assert tuple(STATE_FIELDS) == MOVED_FIELDS


@pytest.mark.parametrize("field,name", MOVED_FIELDS, ids=[field for field, _ in MOVED_FIELDS])
def test_a_field_that_moves_before_the_merge_refuses_by_its_name(rig, field, name):
    moved = {
        "repository": ("nameWithOwner", "octo/renamed"),
        "default": ("default", "trunk"),
        "number": ("number", 8),
        "headRefOid": ("headRefOid", oid("moved")),
        "headRefName": ("headRefName", "feature-2"),
        "headRepository": ("headRepository", "octo/fork"),
        "isCrossRepository": ("isCrossRepository", True),
        "baseRefName": ("baseRefName", "release"),
        "baseRefOid": ("baseRefOid", oid("base-2")),
        "state": ("state", "CLOSED"),
        "isDraft": ("isDraft", True),
        "changedFiles": ("changedFiles", 1),
    }
    doc_field, value = moved[field]
    if field == "changedFiles":
        # Same count, but a different value is needed to move it: grow the list on both reads' terms.
        rig.repo.files = [["src/app.py", None], ["src/two.py", None]]
        rig.repo.files_read2 = rig.repo.files
        value = 3
    rig.repo.state2 = {doc_field: value}
    result = rig.run()
    assert result.code == 2
    assert result.reason == name, result.err
    assert rig.merge_calls() == []


def test_a_file_list_that_moves_before_the_merge_refuses(rig):
    rig.repo.files_read2 = [["src/other.py", None]]
    result = rig.run()
    assert result.reason == "pr_files_moved_before_merge", result.err
    assert rig.merge_calls() == []


@pytest.mark.parametrize("stage", ["item", "receipts", "open_bugs", "hardening", "round_cap"])
def test_records_asked_again_must_answer_the_same(rig, stage):
    if stage == "item":
        rig.port.item_answers = ["it-0123456789ab", "it-ba9876543210"]
    else:
        rig.port.token_answers[stage] = ["first", "second"]
    result = rig.run()
    assert result.reason == "records_moved_before_merge", result.err
    assert rig.merge_calls() == []


def test_records_are_asked_again_not_cached(rig):
    result = rig.run()
    assert result.code == 0, result.err
    for stage in ("item", "receipts", "open_bugs", "hardening", "round_cap"):
        assert rig.port.names().count(stage) == 2


def test_a_grant_withdrawn_between_use_and_merge_refuses(rig):
    """§ 10 row 9 at stage 10: the remediation grant stood at stage 9 and is withdrawn at the recheck."""
    from review_gate.records import remediation_grant_id

    rig.repo.jobs[201] = [{"name": "test", "conclusion": "failure", "steps": []}]
    rig.repo.push_runs["ci.yml"][0]["conclusion"] = "failure"
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = [41, "grant_withdrawn"]
    result = rig.run("--remediation=test")
    assert result.reason == "remediation_not_granted", result.err
    assert rig.merge_calls() == []


def test_a_grant_redecided_between_use_and_merge_refuses(rig):
    from review_gate.records import remediation_grant_id

    rig.repo.jobs[201] = [{"name": "test", "conclusion": "failure", "steps": []}]
    rig.repo.push_runs["ci.yml"][0]["conclusion"] = "failure"
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = [41, 57]
    result = rig.run("--remediation=test")
    assert result.reason == "grant_moved_before_merge", result.err


def test_a_head_behind_the_base_refuses(rig):
    rig.repo.base_compare = [{"status": "diverged", "behind_by": 2, "ahead_by": 1, "base_commit": {"sha": None}}]
    result = rig.run()
    assert result.reason == "base_not_in_head", result.err
    assert rig.merge_calls() == []


@pytest.mark.parametrize("doc,reason", [
    ({"status": "ahead", "behind_by": "0", "ahead_by": 1, "base_commit": {"sha": None}}, "base_compare_unparseable"),
    ({"status": "behind", "behind_by": 0, "ahead_by": 1, "base_commit": {"sha": None}}, "base_compare_unparseable"),
    ({"status": "ahead", "behind_by": 0, "ahead_by": 1, "base_commit": {}}, "base_compare_unparseable"),
    ({"status": "weird", "behind_by": 0, "ahead_by": 1, "base_commit": {"sha": None}}, "base_compare_unparseable"),
    ({"stdout": "", "stderr": "HTTP 500\n", "exit": 1}, "base_compare_failed"),
])
def test_an_unreadable_base_check_refuses(rig, doc, reason):
    rig.repo.base_compare = [doc]
    result = rig.run()
    assert result.reason == reason, result.err


def test_main_moving_between_the_green_proof_and_the_base_check_refuses(rig):
    """Stage 9 judged tip T; the base check's comparison names another tip."""
    rig.repo.base_compare = [{"status": "ahead", "behind_by": 0, "ahead_by": 1, "base_commit": {"sha": oid("new-tip")}}]
    result = rig.run()
    assert result.reason == "main_moved_during_gate", result.err
    assert rig.merge_calls() == []


def test_forged_matrix_row_1_body_lines_change_no_decision(rig):
    """§ 10 row 1: grant, waiver and hardening lines in the body the REST record carries change nothing."""
    body = "\n".join([
        "Review round grant: r4 — owner standing grant 2026-11-01; scope: all",
        "Waiver: src/app.py:1 — this finding is accepted by the owner for this merge",
        "Hardening: src/app.py:2 — TICKET1",
        "Exception: single-family merge granted",
        "Remediation: main is red, merge anyway",
    ])
    # No ready_for_review event, so the REST record (which carries the body) is read.
    rig.repo.timeline = []
    without = rig.run()
    rig2_calls = rig.fake.calls()
    assert without.code == 0, without.err

    from rig import Rig

    second = Rig(rig.tmp_path / "with-body")
    second.repo.timeline = []
    second.repo.pull_record = dict(second.repo.pull_record, body=body)
    second.repo.branch_pulls = [dict(pull, body=body) for pull in second.repo.branch_pulls]
    with_body = second.run()
    assert with_body.code == without.code
    assert with_body.out == without.out
    assert second.port.names() == rig.port.names()
    assert [c[:2] for c in second.fake.calls()] == [c[:2] for c in rig2_calls]
    assert any(call == ["api", "repos/octo/widget/pulls/7"] for call in second.fake.calls())

    # And on a refusal: the same refusal with and without the lines.
    third = Rig(rig.tmp_path / "red-with-body")
    third.repo.timeline = []
    third.repo.pull_record = dict(third.repo.pull_record, body=body)
    third.repo.jobs[101] = [{"name": "test", "conclusion": "failure", "steps": []}]
    fourth = Rig(rig.tmp_path / "red-without-body")
    fourth.repo.timeline = []
    fourth.repo.jobs[101] = [{"name": "test", "conclusion": "failure", "steps": []}]
    assert third.run().err == fourth.run().err


def test_a_ledger_error_from_a_records_stage_is_a_named_refusal(rig):
    def refuse_receipts(subject, item):
        raise LedgerRefusal("missing", "pr_item_missing")

    rig.port.receipts = refuse_receipts
    result = rig.run()
    assert result.code == 2
    assert result.reason == "records_read_failed"
    assert rig.merge_calls() == []
