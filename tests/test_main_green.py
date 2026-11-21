"""Stage 9: main is green at its tip, per selected workflow file, with --remediation an exact set and a grant."""
from __future__ import annotations

from review_gate.records import no_ci_grant_id, remediation_grant_id

from rig import LINT_DOCS_YML, job, key, oid, ok, proof_jobs, run_doc

CI = ".github/workflows/ci.yml"
LINT = ".github/workflows/lint.yml"
OLDER = "2026-11-19T08:00:00Z"
NEWER = "2026-11-19T09:00:00Z"

ALWAYS_LINT_YML = """name: Lint
on:
  push:
    branches: [main]
  pull_request:
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""


def push_run(run_id, path, sha, created=NEWER, conclusion="success", status="completed", branch="main"):
    return run_doc(run_id, path, event="push", head_sha=sha, head_branch=branch, created=created,
                   conclusion=conclusion, status=status)


def newest_across_all_workflows(rig, run):
    """The answer GitHub gives to the old selection (newest push run on main of any workflow)."""
    rig.repo.extra[key(["api", "repos/octo/widget/actions/runs?branch=main&event=push&per_page=1"])] = [
        ok({"workflow_runs": [run]})
    ]


def red_main(rig, *names):
    rig.repo.push_runs["ci.yml"][0]["conclusion"] = "failure"
    rig.repo.jobs[201] = [job(name, "failure") for name in names] + [job("ok job")]


# ---- § 6's three carried cases and § 10 row 12 ----------------------------

def test_carried_a_newer_green_run_of_another_workflow_beside_an_unproven_skip_refuses(rig):
    """§ 6 case 1 and § 10 row 12: main's newest push run is another, green workflow; CI skipped a job unproven."""
    rig.repo.tip_workflows = {"ci.yml": rig.repo.tip_workflows["ci.yml"], "lint.yml": LINT_DOCS_YML}
    rig.repo.push_runs["ci.yml"] = [push_run(201, CI, rig.repo.tip, created=OLDER)]
    rig.repo.jobs[201] = [job("test"), job("long tests", "skipped")]
    lint_run = push_run(202, LINT, rig.repo.tip, created=NEWER)
    lint_run["name"] = "Docs lint"
    rig.repo.push_runs["lint.yml"] = [lint_run]
    rig.repo.jobs[202] = [job("lint")]
    newest_across_all_workflows(rig, lint_run)
    result = rig.run()
    assert result.code == 2
    assert result.reason == "main_tree_proof_missing", result.err
    assert rig.merge_calls() == []


def test_carried_no_push_run_of_the_workflow_refuses(rig):
    """§ 6 case 2: no CI push run refuses main_ci_run_missing; it is never skipped."""
    rig.repo.push_runs["ci.yml"] = []
    lint_run = push_run(202, LINT, rig.repo.tip)
    newest_across_all_workflows(rig, lint_run)
    rig.repo.jobs[202] = [job("lint")]
    result = rig.run()
    assert result.reason == "main_ci_run_missing", result.err
    assert rig.merge_calls() == []


def test_carried_a_clean_run_passes(rig):
    """§ 6 case 3: a clean run passes, a proven skip included."""
    rig.repo.jobs[201] = proof_jobs(rig.repo.tip)
    newest_across_all_workflows(rig, rig.repo.push_runs["ci.yml"][0])
    result = rig.run()
    assert result.code == 0, result.err


# ---- selection and binding -----------------------------------------------

def test_a_run_of_another_workflow_returned_for_the_one_asked_refuses(rig):
    """The fake answers another workflow's run whatever was asked: the post-read field check refuses."""
    rig.repo.push_runs["ci.yml"] = [push_run(202, LINT, rig.repo.tip)]
    result = rig.run()
    assert result.reason == "main_run_misselected", result.err


def test_a_pull_request_run_returned_as_a_push_run_refuses(rig):
    run = push_run(201, CI, rig.repo.tip)
    run["event"] = "pull_request"
    rig.repo.push_runs["ci.yml"] = [run]
    result = rig.run()
    assert result.reason == "main_run_misselected", result.err


def test_a_push_run_of_another_branch_refuses(rig):
    rig.repo.push_runs["ci.yml"] = [push_run(201, CI, rig.repo.tip, branch="release")]
    result = rig.run()
    assert result.reason == "main_run_misselected", result.err


def test_a_stale_newest_run_refuses(rig):
    rig.repo.push_runs["ci.yml"] = [push_run(201, CI, oid("older-tip"))]
    result = rig.run()
    assert result.reason == "main_ci_run_stale", result.err
    assert rig.merge_calls() == []


def test_the_newest_run_is_chosen_by_creation_time_not_list_order(rig):
    rig.repo.push_runs["ci.yml"] = [
        push_run(200, CI, oid("older-tip"), created=OLDER),
        push_run(201, CI, rig.repo.tip, created=NEWER),
    ]
    assert rig.run().code == 0
    rig2 = type(rig)(rig.tmp_path / "second")
    rig2.repo.push_runs["ci.yml"] = [
        push_run(201, CI, rig2.repo.tip, created=OLDER),
        push_run(200, CI, oid("newer-elsewhere"), created=NEWER),
    ]
    assert rig2.run().reason == "main_ci_run_stale"


def test_a_pending_run_refuses(rig):
    rig.repo.push_runs["ci.yml"] = [push_run(201, CI, rig.repo.tip, status="in_progress", conclusion=None)]
    result = rig.run()
    assert result.reason == "main_ci_run_pending", result.err


def test_a_default_branch_not_named_main_is_read_from_the_state(rig):
    workflow = "name: CI\non: [push, pull_request]\njobs: {}\n"
    rig.repo.default = "trunk"
    rig.repo.head_workflows = {"ci.yml": workflow}
    rig.repo.tip_workflows = {"ci.yml": workflow}
    rig.repo.push_runs = {"ci.yml": [push_run(201, CI, rig.repo.tip, branch="trunk")]}
    result = rig.run()
    assert result.code == 0, result.err
    assert any("git/ref/heads/trunk" in call for call in rig.endpoints())
    assert not any("heads/main" in call or "branch=main" in call for call in rig.endpoints())


def test_a_default_branch_not_named_main_refuses_a_stale_run(rig):
    workflow = "name: CI\non: [push, pull_request]\njobs: {}\n"
    rig.repo.default = "trunk"
    rig.repo.head_workflows = {"ci.yml": workflow}
    rig.repo.tip_workflows = {"ci.yml": workflow}
    rig.repo.push_runs = {"ci.yml": [push_run(201, CI, oid("old"), branch="trunk")]}
    result = rig.run()
    assert result.reason == "main_ci_run_stale", result.err


# ---- job grain and the proof ----------------------------------------------

def test_a_success_run_with_a_neutral_job_is_red(rig):
    rig.repo.jobs[201] = [job("test"), job("audit", "neutral")]
    result = rig.run()
    assert result.reason == "main_ci_red", result.err


def test_a_success_run_with_a_cancelled_job_is_red(rig):
    rig.repo.jobs[201] = [job("test"), job("audit", "cancelled")]
    result = rig.run()
    assert result.reason == "main_ci_red", result.err


def test_the_proof_is_read_for_every_selected_workflow_not_only_ci(rig):
    rig.repo.head_workflows = {"ci.yml": rig.repo.head_workflows["ci.yml"], "lint.yml": ALWAYS_LINT_YML}
    rig.repo.tip_workflows = dict(rig.repo.head_workflows)
    rig.repo.pr_runs.append(run_doc(105, LINT, head_sha=rig.repo.head))
    rig.repo.jobs[105] = [job("lint")]
    rig.repo.push_runs["lint.yml"] = [push_run(202, LINT, rig.repo.tip)]
    rig.repo.jobs[202] = [job("lint"), job("slow lint", "skipped")]
    result = rig.run()
    assert result.reason == "main_tree_proof_missing", result.err


def test_a_red_run_with_no_red_job_refuses_unattributed(rig):
    rig.repo.push_runs["ci.yml"][0]["conclusion"] = "failure"
    result = rig.run("--remediation=test")
    assert result.reason == "main_ci_red_unattributed", result.err


# ---- --remediation ---------------------------------------------------------

def test_a_red_main_without_remediation_refuses(rig):
    red_main(rig, "unit")
    result = rig.run()
    assert result.reason == "main_ci_red", result.err
    assert rig.merge_calls() == []


def test_remediation_naming_exactly_the_red_jobs_with_its_grant_merges(rig):
    red_main(rig, "unit", "lint")
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=lint,unit")
    assert result.code == 0, result.err
    assert ("standing", f"grant:remediation:octo/widget#7:{rig.repo.head}") in rig.port.calls


def test_remediation_naming_a_superset_refuses(rig):
    red_main(rig, "unit")
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=unit,lint")
    assert result.reason == "remediation_set_mismatch", result.err


def test_remediation_naming_a_subset_refuses(rig):
    red_main(rig, "unit", "lint")
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=unit")
    assert result.reason == "remediation_set_mismatch", result.err


def test_remediation_naming_a_job_that_is_not_red_refuses(rig):
    red_main(rig, "unit")
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=ok job")
    assert result.reason == "remediation_set_mismatch", result.err


def test_remediation_without_its_grant_refuses(rig):
    red_main(rig, "unit")
    result = rig.run("--remediation=unit")
    assert result.reason == "remediation_not_granted", result.err
    assert "grant_unknown" in result.err
    assert rig.merge_calls() == []


def test_a_remediation_grant_for_another_head_lifts_nothing(rig):
    red_main(rig, "unit")
    rig.port.grants[remediation_grant_id("octo/widget", 7, oid("earlier-head"))] = 77
    result = rig.run("--remediation=unit")
    assert result.reason == "remediation_not_granted", result.err


def test_a_withdrawn_remediation_grant_refuses(rig):
    red_main(rig, "unit")
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = "grant_withdrawn"
    result = rig.run("--remediation=unit")
    assert result.reason == "remediation_not_granted", result.err


def test_remediation_on_a_green_main_refuses(rig):
    result = rig.run("--remediation=unit")
    assert result.reason == "remediation_main_not_red", result.err


def test_remediation_never_lifts_an_unproven_skip(rig):
    rig.repo.push_runs["ci.yml"][0]["conclusion"] = "failure"
    rig.repo.jobs[201] = [job("unit", "failure"), job("long tests", "skipped")]
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=unit")
    assert result.reason == "main_tree_proof_missing", result.err
    assert rig.merge_calls() == []


def test_remediation_never_lifts_a_stale_missing_or_pending_run(rig):
    grant = remediation_grant_id("octo/widget", 7, rig.repo.head)
    cases = [
        ([push_run(201, CI, oid("old"), conclusion="failure")], "main_ci_run_stale"),
        ([], "main_ci_run_missing"),
        ([push_run(201, CI, rig.repo.tip, status="queued", conclusion=None)], "main_ci_run_pending"),
    ]
    for index, (runs, reason) in enumerate(cases):
        current = type(rig)(rig.tmp_path / f"case-{index}")
        current.repo.push_runs["ci.yml"] = runs
        current.repo.jobs[201] = [job("unit", "failure")]
        current.port.grants[grant] = 77
        result = current.run("--remediation=unit")
        assert result.reason == reason, result.err


def test_remediation_never_relaxes_stage_8(rig):
    red_main(rig, "test")
    rig.repo.jobs[101] = [job("test", "failure")]
    rig.port.grants[remediation_grant_id("octo/widget", 7, rig.repo.head)] = 77
    result = rig.run("--remediation=test")
    assert result.reason == "ci_job_not_success", result.err


def test_the_same_red_job_in_two_workflows_is_ambiguous(rig):
    rig.repo.head_workflows = {"ci.yml": rig.repo.head_workflows["ci.yml"], "lint.yml": ALWAYS_LINT_YML}
    rig.repo.tip_workflows = dict(rig.repo.head_workflows)
    rig.repo.pr_runs.append(run_doc(105, LINT, head_sha=rig.repo.head))
    rig.repo.jobs[105] = [job("lint")]
    red_main(rig, "unit")
    rig.repo.push_runs["lint.yml"] = [push_run(202, LINT, rig.repo.tip, conclusion="failure")]
    rig.repo.jobs[202] = [job("unit", "failure")]
    result = rig.run("--remediation=unit")
    assert result.reason == "main_red_job_ambiguous", result.err


def test_a_red_job_with_a_comma_cannot_be_named(rig):
    red_main(rig, "test (3.11, ubuntu)")
    result = rig.run()
    assert result.reason == "main_red_job_unnameable", result.err


def test_the_remediation_grant_id_names_the_head():
    head = oid("h")
    assert remediation_grant_id("Octo/Widget", 7, head) == f"grant:remediation:octo/widget#7:{head}"


# ---- the three workflow shapes ----------------------------------------------

def test_a_workflow_the_pull_request_adds_refuses(rig):
    rig.repo.tip_workflows = {}
    rig.repo.push_runs = {}
    result = rig.run()
    assert result.reason == "main_workflow_added_or_renamed", result.err


def test_a_workflow_the_pull_request_renames_refuses(rig):
    rig.repo.head_workflows = {"build.yml": rig.repo.head_workflows["ci.yml"]}
    rig.repo.pr_runs = [run_doc(101, ".github/workflows/build.yml", head_sha=rig.repo.head)]
    result = rig.run()
    assert result.reason == "main_workflow_added_or_renamed", result.err


def test_a_workflow_with_no_push_trigger_refuses(rig):
    pr_only = "name: CI\non:\n  pull_request:\njobs: {}\n"
    rig.repo.head_workflows = {"ci.yml": pr_only}
    rig.repo.tip_workflows = {"ci.yml": pr_only}
    result = rig.run()
    assert result.reason == "main_workflow_no_push_trigger", result.err


def test_a_push_trigger_on_other_branches_only_refuses(rig):
    other = "name: CI\non:\n  pull_request:\n  push:\n    branches: [release/*]\njobs: {}\n"
    rig.repo.tip_workflows = {"ci.yml": other}
    result = rig.run()
    assert result.reason == "main_workflow_no_push_trigger", result.err


def test_a_path_filtered_push_workflow_whose_tip_started_no_run_refuses(rig):
    filtered = "name: CI\non:\n  pull_request:\n  push:\n    branches: [main]\n    paths: ['src/**']\njobs: {}\n"
    rig.repo.tip_workflows = {"ci.yml": filtered}
    rig.repo.push_runs["ci.yml"] = [push_run(201, CI, oid("an older commit"))]
    result = rig.run()
    assert result.reason == "main_workflow_path_filtered", result.err


def test_a_path_filtered_push_workflow_with_a_run_at_the_tip_passes(rig):
    filtered = "name: CI\non:\n  pull_request:\n  push:\n    branches: [main]\n    paths: ['src/**']\njobs: {}\n"
    rig.repo.tip_workflows = {"ci.yml": filtered}
    result = rig.run()
    assert result.code == 0, result.err


def test_with_the_no_ci_grant_a_tip_with_workflows_refuses(rig):
    rig.repo.head_workflows = {}
    rig.port.grants[no_ci_grant_id("octo/widget")] = 31
    result = rig.run()
    assert result.reason == "main_ci_unchecked", result.err


def test_an_unreadable_tip_refuses(rig):
    rig.repo.extra = {key(["api", "repos/octo/widget/git/ref/heads/main"]): [ok({"ref": "refs/heads/main"})]}
    result = rig.run()
    assert result.reason == "main_tip_unreadable", result.err
