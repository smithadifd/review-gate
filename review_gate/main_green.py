"""Stage 9: the default branch is green at its tip, per workflow file.

The default branch's name comes from the state read, never a literal. Its
tip T is read from the branch's ref. The subjects are the workflows stage 8
selected for this pull request, each by its file path. For each one, in
path order:

- The file must exist at T (``main_workflow_added_or_renamed``: a pull
  request that adds or renames a workflow has no push run of it on main).
- Its ``on`` at T must run on a push to the default branch
  (``main_workflow_no_push_trigger``).
- Main's push runs of that file are read by explicit selection
  (``actions/workflows/<file>/runs?branch=<default>&event=push``), and every
  run returned is checked for its own fields: ``path`` the file asked for,
  event ``push``, ``head_branch`` the default branch and ``repository`` this
  repository (``main_run_misselected`` otherwise; the server-side filter is
  not trusted alone). No run refuses ``main_ci_run_missing``; it is never
  skipped.
- The newest run (by creation time, then id) must be on T, except for a
  path-filtered push workflow: its newest run may be on an ancestor when
  every later commit through T touches no path admitted by the push filter
  at T. A touching commit refuses ``main_workflow_path_filtered``, naming
  its sha and path. An unreadable or capped compare or commit read, doubtful
  ancestry, or a merge commit refuses ``main_filter_commits_unreadable``.
  An unfiltered workflow's run behind T still refuses ``main_ci_run_stale``.
- The run must be completed (``main_ci_run_pending``). Its jobs are read
  through every page (``main_ci_jobs_unreadable``). A skipped job passes only
  with a valid main-tree proof in that run (one job named ``main tree proof``
  with conclusion success, holding exactly one successful step whose name is
  ``Main tree proof: head <sha> run <id>``); otherwise
  ``main_tree_proof_missing``. Every other job must conclude ``success``: a
  failure, a neutral or a cancelled job is red. A run whose conclusion is
  not success with no red job refuses ``main_ci_red_unattributed``.

Every refusal above is checked for every subject before any red job is
considered, and none of them can be lifted by any flag or grant. Then the
red jobs: the same job name red in two selected workflows refuses
``main_red_job_ambiguous``, and a red job whose name has a comma refuses
``main_red_job_unnameable`` (it cannot be named in ``--remediation``). With
no red job, main is green; a ``--remediation`` then refuses
``remediation_main_not_red``. With red jobs and no ``--remediation`` the
stage refuses ``main_ci_red``. ``--remediation`` must name exactly main's
red jobs, compared as sets (``remediation_set_mismatch``), and the grant
``grant:remediation:<owner>/<repo>#<N>:<head>`` must stand
(``remediation_not_granted``). Remediation never relaxes stage 8.

With no workflow file at the head (stage 8 passed on ``grant:no-ci``), the
tip must have none either (``main_ci_unchecked``); there is then no run to
read. The tip T is kept for stage 10, whose base check must compare against
this same tip.

Residual: the main-tree proof reader checks the marker's grammar and
conclusion only; it does not check that the head or run the marker names is
the one the proof was made for.
"""
from __future__ import annotations

import re
import urllib.parse

from .gh import api_json
from .github import OID, blob_text, default_tip, is_count, parse_time, path_problem, run_jobs, same_repository, workflow_files
from .records import ask_grant, remediation_grant_id
from .refusal import refuse
from .workflows import NO_PUSH, PATH_FILTERED, ignore_match, load_workflow, pattern_match, push_path_filter, push_trigger

PROOF_JOB_NAME = "main tree proof"
MARKER = re.compile(r"Main tree proof: head [0-9a-f]{40} run [0-9]+")
RUNS_PER_READ = 20


def _conclusion(item):
    value = item.get("conclusion") if isinstance(item, dict) else None
    return value.lower() if isinstance(value, str) else None


def skip_proven(jobs) -> bool:
    """Exactly one successful ``main tree proof`` job with exactly one successful marker step."""
    if not isinstance(jobs, list):
        return False
    proof = [job for job in jobs if isinstance(job, dict) and job.get("name") == PROOF_JOB_NAME]
    if len(proof) != 1 or _conclusion(proof[0]) != "success":
        return False
    steps = proof[0].get("steps")
    if not isinstance(steps, list):
        return False
    markers = [
        step for step in steps
        if isinstance(step, dict) and isinstance(step.get("name"), str) and MARKER.fullmatch(step["name"])
    ]
    return len(markers) == 1 and _conclusion(markers[0]) == "success"


def newest_push_run(gh, pr, path: str, default: str):
    """The newest push run of workflow file ``path`` on ``default``, each returned run checked; None when none."""
    file_name = path.rsplit("/", 1)[-1]
    endpoint = (
        f"repos/{pr.owner}/{pr.repo}/actions/workflows/{urllib.parse.quote(file_name, safe='')}/runs"
        f"?branch={urllib.parse.quote(default, safe='')}&event=push&per_page={RUNS_PER_READ}"
    )
    doc = api_json(gh, endpoint, "main_ci_runs_unreadable")
    if not isinstance(doc, dict) or not isinstance(doc.get("workflow_runs"), list):
        refuse("main_ci_runs_unreadable", f"the push runs of {path} are malformed")
    newest = None
    for run in doc["workflow_runs"]:
        if not isinstance(run, dict) or not is_count(run.get("id")) or run["id"] == 0:
            refuse("main_ci_runs_unreadable", f"a push run of {path} is malformed")
        if run.get("path") != path:
            refuse("main_run_misselected", f"run {run['id']} is of {run.get('path')!r}, not {path}")
        if run.get("event") != "push":
            refuse("main_run_misselected", f"run {run['id']} of {path} has event {run.get('event')!r}")
        if run.get("head_branch") != default:
            refuse("main_run_misselected", f"run {run['id']} of {path} is on {run.get('head_branch')!r}, not {default}")
        if not same_repository(run.get("repository"), pr.slug):
            refuse("main_run_misselected", f"run {run['id']} of {path} is not this repository's")
        created = parse_time(run.get("created_at"), f"run {run['id']} created_at", "main_ci_runs_unreadable")
        key = (created, run["id"])
        if newest is None or key > newest[0]:
            newest = (key, run)
    return None if newest is None else newest[1]


def _check_filter_commits(gh, pr, path: str, head, tip: str, patterns, ignore) -> None:
    """An exact linear range after ``head`` touches nothing the tip's push filter admits."""
    name = "main_filter_commits_unreadable"
    if not isinstance(head, str) or not OID.fullmatch(head):
        refuse(name, f"{path}: the newest push run has no full head sha")
    endpoint = f"repos/{pr.owner}/{pr.repo}/compare/{head}...{tip}"
    compare = api_json(gh, endpoint, name)
    if not isinstance(compare, dict):
        refuse(name, f"{path}: the comparison is not an object")
    ahead = compare.get("ahead_by")
    behind = compare.get("behind_by")
    if compare.get("status") != "ahead" or not is_count(behind) or behind != 0 or not is_count(ahead) or ahead < 1:
        refuse(name, f"{path}: {head} is not a proven ancestor of {tip}")
    commits = compare.get("commits")
    if not isinstance(commits, list) or len(commits) != ahead or len(commits) >= 250:
        refuse(name, f"{path}: the comparison's commit list is incomplete or capped")
    parent = head
    for entry in commits:
        sha = entry.get("sha") if isinstance(entry, dict) else None
        if not isinstance(sha, str) or not OID.fullmatch(sha):
            refuse(name, f"{path}: the comparison has a commit without a full sha")
        commit = api_json(gh, f"repos/{pr.owner}/{pr.repo}/commits/{sha}", name)
        if not isinstance(commit, dict) or commit.get("sha") != sha:
            refuse(name, f"{path}: commit {sha} is malformed or misbound")
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 1:
            refuse(name, f"{path}: commit {sha} is not a single-parent commit")
        if not isinstance(parents[0], dict) or parents[0].get("sha") != parent:
            refuse(name, f"{path}: commit {sha} does not continue the exact ancestor range")
        files = commit.get("files")
        if not isinstance(files, list) or len(files) >= 300:
            refuse(name, f"{path}: commit {sha} has an unreadable or capped file list")
        for file in files:
            if not isinstance(file, dict):
                refuse(name, f"{path}: commit {sha} has a malformed file")
            for field in ("filename", "previous_filename"):
                if field == "previous_filename" and field not in file:
                    continue
                changed = file.get(field)
                problem = path_problem(changed)
                if problem is not None:
                    refuse(name, f"{path}: commit {sha} has an unreadable {field}: {problem}")
                touches = (
                    any(pattern_match(pattern, changed) for pattern in patterns)
                    if patterns is not None else
                    not any(ignore_match(pattern, changed) for pattern in ignore)
                )
                if touches:
                    refuse("main_workflow_path_filtered", f"{path}: commit {sha} touches {changed!r}, admitted by the push filter at {tip}")
        parent = sha
    if parent != tip:
        refuse(name, f"{path}: the comparison's commits do not reach tip {tip}")


def _judge(gate, path: str, tip_files: dict, tip: str, default: str):
    """(run, red job names) for one subject, after every refusal no grant lifts."""
    pr = gate.args.pr
    if path not in tip_files:
        refuse(
            "main_workflow_added_or_renamed",
            f"{path} is not a workflow file at the default branch tip {tip}, so main has no push run of it",
        )
    doc = load_workflow(path, blob_text(gate.gh, pr, tip_files[path], path))
    trigger = push_trigger(path, doc, default)
    if trigger == NO_PUSH:
        refuse("main_workflow_no_push_trigger", f"{path} at {tip} does not run on a push to {default}")
    run = newest_push_run(gate.gh, pr, path, default)
    if run is None:
        refuse("main_ci_run_missing", f"no push run of {path} on {default}")
    label = f"main's newest push run {run['id']} of {path}"
    if run.get("head_sha") != tip:
        if trigger == PATH_FILTERED:
            patterns, ignore = push_path_filter(path, doc)
            _check_filter_commits(gate.gh, pr, path, run.get("head_sha"), tip, patterns, ignore)
        else:
            refuse("main_ci_run_stale", f"{label} is on {run.get('head_sha')!r}, not the tip {tip}")
    if run.get("status") != "completed":
        refuse("main_ci_run_pending", f"{label} status={run.get('status')!r}")
    jobs = run_jobs(gate.gh, pr, run["id"], "main_ci_jobs_unreadable")
    if not jobs:
        refuse("main_ci_jobs_unreadable", f"{label} has no jobs")
    for job in jobs:
        if not isinstance(job.get("name"), str) or not job["name"]:
            refuse("main_ci_jobs_unreadable", f"{label} has a job with no name")
    skipped = [job["name"] for job in jobs if _conclusion(job) == "skipped"]
    if skipped and not skip_proven(jobs):
        refuse("main_tree_proof_missing", f"{label} skipped {', '.join(skipped)} with no valid main tree proof")
    red = [job["name"] for job in jobs if _conclusion(job) not in ("success", "skipped")]
    if run.get("conclusion") != "success" and not red:
        refuse("main_ci_red_unattributed", f"{label} conclusion={run.get('conclusion')!r} with no red job")
    return run, red


def stage_main_green(gate) -> None:
    pr = gate.args.pr
    default = gate.state["default"]
    tip = default_tip(gate.gh, pr, default)
    tip_files = workflow_files(gate.gh, pr, tip)
    if gate.no_ci_grant is not None:
        if tip_files:
            refuse(
                "main_ci_unchecked",
                f"the head has no workflow file but the default branch tip {tip} has {len(tip_files)}",
            )
        gate.tip = tip
        return
    judged = [(path, *_judge(gate, path, tip_files, tip, default)) for path in sorted(gate.expected)]
    red_jobs = {}
    for path, _run, red in judged:
        for name in red:
            if name in red_jobs:
                refuse("main_red_job_ambiguous", f"job {name!r} is red in {red_jobs[name]} and in {path}")
            red_jobs[name] = path
    for name in sorted(red_jobs):
        if "," in name:
            refuse("main_red_job_unnameable", f"red job {name!r} has a comma, so --remediation cannot name it")
    remediation = gate.args.remediation
    if not red_jobs:
        if remediation is not None:
            refuse("remediation_main_not_red", f"main is green at {tip}; --remediation names {sorted(remediation)}")
        gate.tip = tip
        return
    if remediation is None:
        refuse(
            "main_ci_red",
            f"main is red at {tip}: {', '.join(sorted(red_jobs))}; a remediation merge names exactly these "
            "with --remediation= and needs its grant",
        )
    if remediation != frozenset(red_jobs):
        refuse(
            "remediation_set_mismatch",
            f"--remediation names {sorted(remediation)}, main's red jobs at {tip} are {sorted(red_jobs)}",
        )
    gate.remediation_grant = ask_grant(
        gate.records,
        remediation_grant_id(pr.slug, pr.number, gate.args.sha),
        "remediation_not_granted",
        f"main is red at {tip}",
    )
    gate.tip = tip
