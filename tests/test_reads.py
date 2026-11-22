"""Reads that fail or come back malformed refuse by name; nothing is skipped."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from review_gate.gh import GhTransport
from review_gate.refusal import Refusal
from review_gate.workflows import load_workflow, selected_workflows

from rig import key, make_store, ok


def compare_argv(rig):
    from review_gate.github import COMPARE_FILES_JQ

    return ["api", "--jq", COMPARE_FILES_JQ, f"repos/octo/widget/compare/{rig.repo.base_oid}...{rig.repo.head}"]


def test_no_gh_on_path_refuses_gh_unavailable():
    with pytest.raises(Refusal) as refused:
        GhTransport(None).run(["api", "x"])
    assert refused.value.name == "gh_unavailable"


def test_a_gh_that_cannot_start_refuses_gh_unavailable(tmp_path):
    with pytest.raises(Refusal) as refused:
        GhTransport(str(tmp_path / "no-such-gh")).run(["api", "x"])
    assert refused.value.name == "gh_unavailable"


def test_a_stalled_gh_refuses_by_timeout(tmp_path):
    fake = tmp_path / "gh"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(1)\n", encoding="utf-8")
    fake.chmod(0o755)
    probe = (
        "import sys\n"
        "from review_gate.gh import GhTransport\n"
        "from review_gate.refusal import Refusal\n"
        "try:\n"
        "    GhTransport(sys.argv[1], timeout=0.05).run(['api', 'x'])\n"
        "except Refusal as exc:\n"
        "    assert exc.name == 'gh_timeout', exc.name\n"
        "else:\n"
        "    raise AssertionError('gh_timeout was not raised')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(fake)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=5,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("lines", [
    "not json\n",
    '["src/app.py"]\n',
    '["../escape.py", null]\n',
    '["/abs.py", null]\n',
    '["src/app.py", null]\n["src/app.py", null]\n',
])
def test_a_malformed_file_list_refuses_pr_files_unresolved(rig, lines):
    rig.repo.state1 = {"changedFiles": 1}
    rig.repo.extra = {key(compare_argv(rig)): [ok(lines)]}
    result = rig.run()
    assert result.reason == "pr_files_unresolved", result.err
    assert rig.merge_calls() == []


def test_a_failed_file_list_read_refuses_pr_files_unresolved(rig):
    rig.repo.extra = {key(compare_argv(rig)): [{"stdout": "", "stderr": "HTTP 500\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "pr_files_unresolved", result.err


def test_an_empty_file_list_refuses_pr_files_unresolved(rig):
    rig.repo.state1 = {"changedFiles": 0}
    rig.repo.extra = {key(compare_argv(rig)): [ok("")]}
    result = rig.run()
    assert result.reason == "pr_files_unresolved", result.err


def test_a_truncated_tree_refuses_workflow_files_unreadable(rig):
    routes = rig.repo.routes()
    commit_key = key(["api", f"repos/octo/widget/git/commits/{rig.repo.head}"])
    import json

    root = json.loads(routes[commit_key][0]["stdout"])["tree"]["sha"]
    root_key = key(["api", f"repos/octo/widget/git/trees/{root}"])
    doc = json.loads(routes[root_key][0]["stdout"])
    doc["truncated"] = True
    rig.repo.extra = {root_key: [ok(doc)]}
    result = rig.run()
    assert result.reason == "workflow_files_unreadable", result.err


def test_an_unreadable_commit_refuses_workflow_files_unreadable(rig):
    rig.repo.extra = {key(["api", f"repos/octo/widget/git/commits/{rig.repo.head}"]): [
        {"stdout": "", "stderr": "HTTP 404\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "workflow_files_unreadable", result.err


def test_a_workflows_entry_that_is_not_a_directory_refuses(rig):
    import json

    routes = rig.repo.routes()
    commit_key = key(["api", f"repos/octo/widget/git/commits/{rig.repo.head}"])
    root = json.loads(routes[commit_key][0]["stdout"])["tree"]["sha"]
    root_doc = json.loads(routes[key(["api", f"repos/octo/widget/git/trees/{root}"])][0]["stdout"])
    github = next(entry for entry in root_doc["tree"] if entry["path"] == ".github")["sha"]
    github_key = key(["api", f"repos/octo/widget/git/trees/{github}"])
    doc = json.loads(routes[github_key][0]["stdout"])
    doc["tree"][0]["type"] = "blob"
    rig.repo.extra = {github_key: [ok(doc)]}
    result = rig.run()
    assert result.reason == "workflow_paths_unreadable", result.err


def test_an_unreadable_push_run_list_refuses_main_ci_runs_unreadable(rig):
    endpoint = "repos/octo/widget/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=20"
    rig.repo.extra = {key(["api", endpoint]): [{"stdout": "", "stderr": "HTTP 500\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "main_ci_runs_unreadable", result.err


def test_a_malformed_push_run_list_refuses_main_ci_runs_unreadable(rig):
    endpoint = "repos/octo/widget/actions/workflows/ci.yml/runs?branch=main&event=push&per_page=20"
    rig.repo.extra = {key(["api", endpoint]): [ok({"workflow_runs": "nope"})]}
    result = rig.run()
    assert result.reason == "main_ci_runs_unreadable", result.err


def test_an_unreadable_main_job_list_refuses_main_ci_jobs_unreadable(rig):
    rig.repo.extra = {key(["api", "--paginate", "--slurp",
                           "repos/octo/widget/actions/runs/201/jobs?per_page=100"]): [
        {"stdout": "", "stderr": "HTTP 500\n", "exit": 1}]}
    result = rig.run()
    assert result.reason == "main_ci_jobs_unreadable", result.err


def test_an_empty_main_job_list_refuses_main_ci_jobs_unreadable(rig):
    rig.repo.jobs[201] = []
    result = rig.run()
    assert result.reason == "main_ci_jobs_unreadable", result.err


def test_an_empty_base_name_refuses_workflow_base_unresolved():
    docs = {".github/workflows/ci.yml": load_workflow("ci", "on: pull_request\n")}
    with pytest.raises(Refusal) as refused:
        selected_workflows(docs, ["a"], "")
    assert refused.value.name == "workflow_base_unresolved"


@pytest.mark.parametrize("answer", [None, "", "not an id!", 7])
def test_a_records_port_item_outside_the_grammar_refuses(rig, answer):
    rig.port.item_answers = [answer]
    result = rig.run()
    assert result.reason == "records_answer_invalid", result.err
    assert rig.merge_calls() == []


def test_a_records_port_token_of_none_refuses(rig):
    rig.port.token_answers["hardening"] = [None]
    result = rig.run()
    assert result.reason == "records_answer_invalid", result.err


def test_an_unknown_write_ahead_state_refuses(rig):
    rig.port.state = "in-progress"
    result = rig.run()
    assert result.reason == "records_answer_invalid", result.err
    assert rig.merge_calls() == []


def test_a_missing_store_directory_refuses_receipt_store_missing(tmp_path):
    store = make_store(tmp_path)
    from review_gate.records import DatabasePaths

    gone = Path(os.path.realpath(tmp_path)) / "gone" / "ledger.db"
    with pytest.raises(Refusal) as refused:
        store.check.check(DatabasePaths(str(gone), str(gone) + ".documents"))
    assert refused.value.name == "receipt_store_missing"


def test_an_unlistable_directory_inside_the_store_refuses_receipt_store_unreadable(tmp_path):
    store = make_store(tmp_path)
    locked = store.root / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(Refusal) as refused:
            store.check.check(store.paths)
    finally:
        locked.chmod(0o700)
    assert refused.value.name == "receipt_store_unreadable"


def test_a_store_behind_an_unsearchable_directory_refuses_receipt_store_unresolvable(tmp_path):
    base = Path(os.path.realpath(tmp_path)) / "closed"
    base.mkdir()
    store = make_store(base)
    base.chmod(0o600)
    try:
        with pytest.raises(Refusal) as refused:
            store.check.check(store.paths)
    finally:
        base.chmod(0o700)
    assert refused.value.name == "receipt_store_unresolvable"


def test_a_stray_os_error_in_the_store_check_refuses_receipt_reader_failed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    from review_gate import store as store_module

    def broken(*_args, **_kwargs):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(store_module.os.path, "realpath", broken)
    with pytest.raises(Refusal) as refused:
        store.check.check(store.paths)
    assert refused.value.name == "receipt_reader_failed"
