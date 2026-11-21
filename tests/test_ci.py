"""Stage 8: post-READY runs of this pull request, judged at job grain."""
from __future__ import annotations

import sys

from review_gate.records import no_ci_grant_id

from rig import AFTER_READY, BEFORE_READY, LATER, LINT_DOCS_YML, READY_AT, job, run_doc

CI = ".github/workflows/ci.yml"
LINT = ".github/workflows/lint.yml"

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


def test_a_green_push_run_on_the_same_sha_does_not_count(rig):
    rig.repo.pr_runs = [run_doc(101, CI, event="push", head_sha=rig.repo.head)]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err
    assert rig.merge_calls() == []


def test_a_run_of_another_head_branch_does_not_count(rig):
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, head_branch="other")]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err


def test_a_run_from_a_fork_head_repository_does_not_count(rig):
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, head_repo="someone/widget")]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err


def test_the_runs_pull_requests_list_is_never_read(rig):
    run = run_doc(101, CI, event="push", head_sha=rig.repo.head)
    run["pull_requests"] = [{"number": 7, "head": {"ref": "feature", "sha": rig.repo.head}}]
    rig.repo.pr_runs = [run]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err


def test_a_run_created_at_ready_does_not_count(rig):
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, created=READY_AT)]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err


def test_a_run_created_before_ready_does_not_count(rig):
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, created=BEFORE_READY)]
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err


def test_a_skipped_job_is_not_success(rig):
    rig.repo.jobs[101] = [job("test"), job("long tests", "skipped")]
    result = rig.run()
    assert result.reason == "ci_job_not_success", result.err
    assert rig.merge_calls() == []


def test_a_success_run_with_a_neutral_or_cancelled_job_refuses(rig):
    for conclusion in ("neutral", "cancelled"):
        rig.repo.jobs[101] = [job("test"), job("extra", conclusion)]
        result = rig.run()
        assert result.reason == "ci_job_not_success", result.err


def test_an_older_failed_run_refuses_beside_a_newer_green_run_of_the_same_workflow(rig):
    rig.repo.pr_runs = [
        run_doc(101, CI, head_sha=rig.repo.head, created=AFTER_READY, conclusion="failure"),
        run_doc(102, CI, head_sha=rig.repo.head, created=LATER),
    ]
    rig.repo.jobs[101] = [job("test", "failure")]
    rig.repo.jobs[102] = [job("test")]
    result = rig.run()
    assert result.reason == "ci_run_not_green", result.err
    assert rig.merge_calls() == []


def test_a_pending_run_refuses(rig):
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, status="in_progress", conclusion=None)]
    result = rig.run()
    assert result.reason == "ci_run_pending", result.err


def test_a_green_run_of_another_workflow_does_not_stand_in_for_one_that_never_started(rig):
    rig.repo.head_workflows = {"ci.yml": rig.repo.head_workflows["ci.yml"], "lint.yml": ALWAYS_LINT_YML}
    rig.repo.pr_runs = [run_doc(105, LINT, head_sha=rig.repo.head)]
    rig.repo.jobs[105] = [job("lint")]
    result = rig.run()
    assert result.reason == "ci_workflow_missing_run", result.err
    assert "CI" in result.err
    assert rig.merge_calls() == []


def test_no_runs_refuses(rig):
    rig.repo.pr_runs = []
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err
    assert rig.merge_calls() == []


def test_a_failed_job_in_an_unselected_workflow_still_refuses(rig):
    rig.repo.head_workflows = {"ci.yml": rig.repo.head_workflows["ci.yml"], "lint.yml": LINT_DOCS_YML}
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head), run_doc(106, LINT, head_sha=rig.repo.head)]
    rig.repo.jobs[106] = [job("lint", "failure")]
    result = rig.run()
    assert result.reason == "ci_job_not_success", result.err


def test_no_workflow_file_without_the_grant_refuses_unchecked_head(rig):
    rig.repo.head_workflows = {}
    rig.repo.tip_workflows = {}
    result = rig.run()
    assert result.reason == "unchecked_head", result.err
    assert "grant_unknown" in result.err
    assert rig.merge_calls() == []
    assert ("standing", no_ci_grant_id("octo/widget")) in rig.port.calls


def test_no_workflow_file_with_a_withdrawn_grant_refuses_unchecked_head(rig):
    rig.repo.head_workflows = {}
    rig.repo.tip_workflows = {}
    rig.port.grants[no_ci_grant_id("octo/widget")] = "grant_withdrawn"
    result = rig.run()
    assert result.reason == "unchecked_head", result.err
    assert "grant_withdrawn" in result.err


def test_no_workflow_file_with_a_standing_grant_merges(rig):
    rig.repo.head_workflows = {}
    rig.repo.tip_workflows = {}
    rig.port.grants[no_ci_grant_id("octo/widget")] = 31
    result = rig.run()
    assert result.code == 0, result.err
    assert not any("actions/runs?head_sha" in call for call in rig.endpoints())
    assert rig.port.names().count("standing") == 2


def test_the_no_ci_grant_is_never_asked_when_a_workflow_file_exists(rig):
    rig.port.grants[no_ci_grant_id("octo/widget")] = 31
    rig.repo.pr_runs = []
    result = rig.run()
    assert result.reason == "ci_no_runs", result.err
    assert "standing" not in rig.port.names()


def test_the_no_ci_grant_id_is_casefolded_and_exact():
    assert no_ci_grant_id("Octo/Widget") == "grant:no-ci:octo/widget"


def test_a_workflow_the_paths_select_none_of_refuses(rig):
    rig.repo.head_workflows = {"lint.yml": LINT_DOCS_YML}
    result = rig.run()
    assert result.reason == "ci_no_workflow_selected", result.err


def test_an_unreadable_trigger_refuses_and_never_drops_the_workflow(rig):
    rig.repo.head_workflows = {"ci.yml": rig.repo.head_workflows["ci.yml"],
                               "odd.yml": "on:\n  pull_request:\n    paths: ['!docs/**']\njobs: {}\n"}
    result = rig.run()
    assert result.reason == "workflow_paths_unreadable", result.err


def test_a_duplicate_key_in_a_workflow_refuses(rig):
    rig.repo.head_workflows = {"ci.yml": "on: push\non: pull_request\njobs: {}\n"}
    result = rig.run()
    assert result.reason == "workflow_paths_unreadable", result.err


def test_pyyaml_absence_refuses_by_name(rig, monkeypatch):
    from review_gate import workflows

    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setattr(workflows, "_LOADER", None)
    result = rig.run()
    assert result.reason == "pyyaml_missing", result.err
    assert rig.merge_calls() == []


def test_a_convert_to_draft_at_or_after_ready_refuses(rig):
    rig.repo.timeline = [
        {"event": "ready_for_review", "created_at": READY_AT},
        {"event": "convert_to_draft", "created_at": READY_AT},
    ]
    result = rig.run()
    assert result.reason == "ci_ready_unresolved", result.err


def test_with_no_draft_events_the_creation_time_is_ready(rig):
    rig.repo.timeline = []
    rig.repo.pull_record["created_at"] = BEFORE_READY
    rig.repo.pr_runs = [run_doc(101, CI, head_sha=rig.repo.head, created=READY_AT)]
    result = rig.run()
    assert result.code == 0, result.err


def test_opened_as_draft_with_no_ready_event_refuses(rig):
    rig.repo.timeline = []
    rig.repo.pull_record["draft"] = True
    result = rig.run()
    assert result.reason == "ci_ready_unresolved", result.err


def test_a_head_branch_shared_with_another_open_pull_request_refuses(rig):
    rig.repo.branch_pulls = [
        {"number": 7, "state": "open", "head": {"ref": "feature"}},
        {"number": 9, "state": "open", "head": {"ref": "feature"}},
    ]
    result = rig.run()
    assert result.reason == "ci_head_branch_shared", result.err


def test_a_failed_jobs_read_refuses(rig):
    from rig import key

    rig.repo.extra = {key(["api", "--paginate", "--slurp",
                           "repos/octo/widget/actions/runs/101/jobs?per_page=100"]): [
        {"stdout": "", "stderr": "HTTP 500\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "ci_read_failed", result.err


def test_jobs_are_read_through_every_page(rig):
    from rig import key, ok

    rig.repo.extra = {key(["api", "--paginate", "--slurp",
                           "repos/octo/widget/actions/runs/101/jobs?per_page=100"]): [
        ok([{"jobs": [job("test")]}, {"jobs": [job("second page", "failure")]}])]}
    result = rig.run()
    assert result.reason == "ci_job_not_success", result.err
    assert "second page" in result.err


def test_required_jobs_are_a_library_argument_only(rig):
    """The library takes a required-job set; the command line has no way to pass one."""
    from review_gate import ci
    import inspect

    assert "required_jobs" in inspect.signature(ci.post_ready_runs_green).parameters
    rig.repo.jobs[101] = [job("test"), job("long tests", "skipped")]
    result = rig.run("--required-jobs=test")
    assert result.reason == "argument_unknown"


def test_a_required_job_set_lets_others_skip_in_the_library(rig):
    from review_gate import ci
    from review_gate.github import ready_time
    from review_gate.grammar import parse_pr_url

    rig.repo.jobs[101] = [job("test"), job("long tests", "skipped")]
    rig.fake.set_routes(rig.repo.routes())
    pr = parse_pr_url(rig.repo.url)
    gh = rig.fake.transport()
    ready = ready_time(gh, pr)
    runs = ci.post_ready_runs_green(gh, pr, rig.repo.head, "feature", {CI: "CI"}, ready,
                                    required_jobs={CI: {"test"}})
    assert [run["id"] for run in runs] == [101]
