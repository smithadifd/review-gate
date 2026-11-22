"""Stage 8: the pinned head is green at job grain on runs of this pull request created after READY.

Workflow files are read at the head by its exact sha. With no workflow file
there is no run to read, and the stage refuses ``unchecked_head`` unless the
standing grant ``grant:no-ci:<owner>/<repo>`` (asked through the records
port, and asked only on this branch) lifts it. A repository whose head has
a workflow file never asks for that grant, so a standing ``grant:no-ci``
lifts nothing there.

With workflow files, the expected workflows are those the pull request's
paths select at the head (``workflows.selected_workflows``); none selected
refuses ``ci_no_workflow_selected``. Then ``post_ready_runs_green``:

- A run counts for this pull request only by its trigger fields: event
  ``pull_request``, ``head_sha`` the pinned head, ``head_branch`` the pull
  request's head branch, and ``head_repository`` and ``repository`` both the
  base repository. The run's ``pull_requests`` list is never read. A run
  created at or before READY does not count (strictly after).
- No counted run refuses ``ci_no_runs``. The head branch must be this pull
  request's alone (``ci_head_branch_shared``).
- Every counted run is judged, not only the newest of its workflow: each
  must be completed (``ci_run_pending``) with conclusion success
  (``ci_run_not_green``), and have at least one job. With no required-job
  set for its workflow, every job must conclude ``success``; a skipped,
  neutral or cancelled job refuses ``ci_job_not_success``. A required-job set
  (``required_jobs``, a library argument the command line cannot set) makes
  those jobs required and lets the others be skipped.
- Every expected workflow must have a counted run
  (``ci_workflow_missing_run``), so a green run of one workflow never
  stands in for another that never started.

Any read that fails refuses by name (``ci_read_failed``,
``workflow_files_unreadable``, ``workflow_paths_unreadable``); nothing is
skipped.
"""
from __future__ import annotations

from .gh import api_pages
from .github import (
    blob_text,
    head_branch_problem,
    is_count,
    parse_time,
    ready_time,
    run_jobs,
    same_repository,
    workflow_files,
)
from .records import ask_grant, no_ci_grant_id
from .refusal import refuse
from .workflows import load_workflow, selected_workflows


def binding_problem(run: dict, sha: str, branch: str, slug: str) -> str | None:
    """Why ``run`` is not bound to this pull request's head by its trigger fields, or None."""
    if run.get("event") != "pull_request":
        return f"event {run.get('event')!r}"
    if run.get("head_sha") != sha:
        return f"head_sha {run.get('head_sha')!r}"
    if run.get("head_branch") != branch:
        return f"head_branch {run.get('head_branch')!r}"
    if not same_repository(run.get("head_repository"), slug):
        return "head_repository"
    if not same_repository(run.get("repository"), slug):
        return "repository"
    return None


def post_ready_runs_green(gh, pr, sha: str, head_branch: str, expected: dict, ready, required_jobs=None) -> list:
    """The judged runs, when every one is green at job grain and every expected workflow ran; else a refusal."""
    endpoint = f"repos/{pr.owner}/{pr.repo}/actions/runs?head_sha={sha}&per_page=100"
    runs = []
    for page in _run_pages(gh, endpoint):
        for run in page["workflow_runs"]:
            if not isinstance(run, dict) or not is_count(run.get("id")) or run["id"] == 0:
                refuse("ci_read_failed", "a workflow run is malformed")
            if binding_problem(run, sha, head_branch, pr.slug) is not None:
                continue
            created = parse_time(run.get("created_at"), f"run {run['id']} created_at", "ci_read_failed")
            if created > ready:
                runs.append(run)
    if not runs:
        refuse("ci_no_runs", f"no workflow run bound to this pull request on {sha} was created after READY ({ready.isoformat()})")
    problem = head_branch_problem(gh, pr, head_branch, ready)
    if problem is not None:
        refuse("ci_head_branch_shared", problem)
    for run in runs:
        run_id = run["id"]
        label = f"workflow run {run_id} ({run.get('path')})"
        if run.get("status") != "completed":
            refuse("ci_run_pending", f"{label} status={run.get('status')!r}")
        if run.get("conclusion") != "success":
            refuse("ci_run_not_green", f"{label} conclusion={run.get('conclusion')!r}")
        jobs = run_jobs(gh, pr, run_id, "ci_read_failed")
        if not jobs:
            refuse("ci_read_failed", f"{label} has no jobs")
        path = run.get("path")
        required = None
        if required_jobs is not None and isinstance(path, str):
            required = required_jobs.get(path)
        if required is not None:
            names = [job.get("name") for job in jobs]
            for required_name in sorted(required):
                if required_name not in names:
                    refuse("ci_job_not_success", f"{label} required job {required_name} is missing")
        for job in jobs:
            name = job.get("name")
            if required is None or not isinstance(name, str) or name in required:
                allowed = ("success",)
            else:
                allowed = ("success", "skipped")
            if job.get("conclusion") not in allowed:
                refuse("ci_job_not_success", f"{label} job {name!r} conclusion={job.get('conclusion')!r}")
    ran = {run.get("path") for run in runs}
    for path, name in sorted(expected.items()):
        if path not in ran:
            refuse("ci_workflow_missing_run", f"workflow {name} ({path}) has no run of this pull request on {sha} after READY")
    return runs


def _run_pages(gh, endpoint: str) -> list:
    pages = api_pages(gh, endpoint, "ci_read_failed")
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            refuse("ci_read_failed", "a workflow-runs page is malformed")
    return pages


def stage_ci(gate) -> None:
    pr, sha = gate.args.pr, gate.args.sha
    files = workflow_files(gate.gh, pr, sha)
    if not files:
        gate.no_ci_grant = ask_grant(
            gate.records, no_ci_grant_id(pr.slug), "unchecked_head", f"no workflow file at {sha}"
        )
        gate.expected = {}
        return
    docs = {path: load_workflow(path, blob_text(gate.gh, pr, files[path], path)) for path in sorted(files)}
    expected = selected_workflows(docs, gate.paths, gate.state["default"])
    if not expected:
        refuse(
            "ci_no_workflow_selected",
            f"the pull request's paths select none of the {len(docs)} workflow file(s) at {sha}",
        )
    ready = ready_time(gate.gh, pr)
    post_ready_runs_green(gate.gh, pr, sha, gate.state["headRefName"], expected, ready, required_jobs=None)
    gate.expected = expected
