"""The no-person scan, run on scratch repositories that hold a copy of the script."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-no-person.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
WORD = "zebra" + "fish"
CLEAN_ID = ("Test Author", "author@example.invalid")
# The scratch repositories carry their own allowance file: only a comment header, no rows.
# They never copy the repository's live file, so a real row there can never read as stale in a scratch scan.
ALLOWANCE_FIXTURE = (
    "# path\tsha256 of the lowercased matched text\treason\n"
    "# A row allows one deny-list match in one path. The pseudo path :commit-identity allows a match in a commit's author or committer identity.\n"
)


def git(repo: Path, *args, identity=CLEAN_ID) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": identity[0],
        "GIT_AUTHOR_EMAIL": identity[1],
        "GIT_COMMITTER_NAME": identity[0],
        "GIT_COMMITTER_EMAIL": identity[1],
    }
    done = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return done.stdout


def scratch(tmp_path: Path, files=None, message="chore: start", identity=CLEAN_ID) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "check-no-person.py")
    (repo / "scripts" / "no-person-allowance.tsv").write_text(ALLOWANCE_FIXTURE, encoding="utf-8")
    for name, text in (files or {"README.md": "a clean file\n"}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, identity=identity)
    return repo


def scan(repo: Path, deny: str | None = WORD, ci: bool = True):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(repo.parent)}
    if ci:
        env["CI"] = "true"
    if deny is not None:
        env["NO_PERSON_DENY"] = deny
    done = subprocess.run([sys.executable, "-I", str(repo / "scripts" / "check-no-person.py")],
                          env=env, capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def test_a_clean_repository_passes(tmp_path):
    code, output = scan(scratch(tmp_path))
    assert code == 0, output
    assert "no-person: ok: 3 tracked files and 1 commits scanned; deny list set" in output


def test_a_planted_word_in_a_file_fails(tmp_path):
    code, output = scan(scratch(tmp_path, {"README.md": f"made by {WORD.title()}\n"}))
    assert code == 1
    assert "README.md:1: deny-list" in output.splitlines()
    assert WORD not in output.lower()
    assert output.count("README.md:1: deny-list") == 1, "the index and HEAD were both scanned"


def test_a_staged_cleanup_does_not_hide_the_head_blob(tmp_path):
    repo = scratch(tmp_path, {"notes.md": f"made by {WORD.title()}\n"})
    head = git(repo, "rev-parse", "HEAD").strip()
    (repo / "notes.md").write_text("clean now\n", encoding="utf-8")
    git(repo, "add", "-A")
    code, output = scan(repo)
    assert code == 1, "the staged cleanup hid the reachable HEAD blob"
    assert f"commit {head[:12]}: notes.md:1: deny-list" in output.splitlines()
    assert WORD not in output.lower()


def test_a_word_removed_from_the_tree_still_fails_in_history(tmp_path):
    repo = scratch(tmp_path, {"notes.md": f"made by {WORD.title()}\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    (repo / "notes.md").write_text("clean now\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: remove old content")
    code, output = scan(repo)
    assert code == 1, "reachable historical blob was accepted"
    assert f"commit {planted[:12]}: notes.md:1: deny-list" in output.splitlines()
    assert WORD not in output.lower()


def test_a_planted_home_path_fails(tmp_path):
    code, output = scan(scratch(tmp_path, {"notes.md": "see /Us" + "ers/someone/x\n"}))
    assert code == 1
    assert "notes.md:1: no-person shape: absolute home" in output


def test_a_planted_word_in_a_path_name_fails(tmp_path):
    code, output = scan(scratch(tmp_path, {f"docs/{WORD}.md": "x\n"}))
    assert code == 1, "a current deny-list path was accepted"
    assert "tracked path #1: deny-list (path name)" in output.splitlines()
    name_was_redacted = WORD not in output.lower()
    assert name_was_redacted, "the current deny-list name was printed"


def test_a_token_path_renamed_away_fails_without_printing_the_name(tmp_path):
    token = "gh" + "p_" + "A" * 36
    path = f"docs/{token}.md"
    repo = scratch(tmp_path, {path: "clean contents\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "mv", path, "docs/clean.md")
    git(repo, "commit", "-q", "-m", "docs: rename")
    code, output = scan(repo)
    assert code == 1, "a historical token-shaped path was accepted"
    name_was_redacted = token not in output
    assert name_was_redacted, "the historical token-shaped name was printed"
    assert f"commit {planted[:12]}: historical path (redacted): path name: secret shape: token" in output.splitlines()


def test_a_current_token_path_fails_without_printing_the_name(tmp_path):
    token = "gh" + "p_" + "A" * 36
    code, output = scan(scratch(tmp_path, {f"docs/{token}.md": "clean contents\n"}))
    assert code == 1, "a current token-shaped path was accepted"
    name_was_redacted = token not in output
    assert name_was_redacted, "the current token-shaped name was printed"
    assert "tracked path #1: path name: secret shape: token" in output.splitlines()


def test_a_deny_list_path_renamed_away_fails_without_printing_the_name(tmp_path):
    path = f"docs/{WORD}.md"
    repo = scratch(tmp_path, {path: "clean contents\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "mv", path, "docs/clean.md")
    git(repo, "commit", "-q", "-m", "docs: rename")
    code, output = scan(repo)
    assert code == 1, "a historical deny-list path was accepted"
    assert f"commit {planted[:12]}: historical path (redacted): deny-list (path name)" in output.splitlines()
    name_was_redacted = WORD not in output.lower()
    assert name_was_redacted, "the historical deny-list name was printed"


def test_a_home_shaped_path_renamed_away_fails(tmp_path):
    path = "docs/x-Us" + "ers-example.md"
    repo = scratch(tmp_path, {path: "clean contents\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "mv", path, "docs/clean.md")
    git(repo, "commit", "-q", "-m", "docs: rename")
    code, output = scan(repo)
    assert code == 1, "a historical home-shaped path was accepted"
    name_was_redacted = path not in output
    assert name_was_redacted, "the historical home-shaped name was printed"
    assert f"commit {planted[:12]}: historical path (redacted): path name: no-person shape: encoded home" in output.splitlines()


def test_a_current_home_shaped_path_fails_without_printing_the_name(tmp_path):
    path = "docs/x-Us" + "ers-example.md"
    code, output = scan(scratch(tmp_path, {path: "clean contents\n"}))
    assert code == 1, "a current home-shaped path was accepted"
    name_was_redacted = path not in output
    assert name_was_redacted, "the current home-shaped name was printed"
    assert "tracked path #1: path name: no-person shape: encoded home" in output.splitlines()


def test_a_current_submodule_refuses(tmp_path):
    repo = scratch(tmp_path)
    target = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{target},vendor/component")
    git(repo, "commit", "-q", "-m", "chore: add component")
    code, output = scan(repo)
    assert code == 1, "a current submodule was accepted"
    assert "vendor/component: unreadable (a submodule entry, not a file)" in output.splitlines()


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("name_rule", ["shape", "secret", "deny"])
def test_content_findings_under_hidden_names_do_not_print_the_name(tmp_path, historical, name_rule):
    names = {
        "shape": "x-Us" + "ers-example",
        "secret": "gh" + "p_" + "A" * 36,
        "deny": WORD,
    }
    name = names[name_rule]
    path = f"docs/{name}.md"
    repo = scratch(tmp_path, {path: f"made by {WORD.title()}\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    if historical:
        git(repo, "rm", "-q", path)
        git(repo, "commit", "-q", "-m", "docs: remove old content")
    code, output = scan(repo)
    assert code == 1, "content under a hidden name was accepted"
    name_was_redacted = name.lower() not in output.lower()
    assert name_was_redacted, "a hidden name was printed on a content line"
    label = f"commit {planted[:12]}: historical path (redacted)" if historical else "tracked path #1"
    assert f"{label}:1: deny-list" in output.splitlines()


@pytest.mark.parametrize("allow", [True, False])
def test_a_historical_blob_uses_its_path_allowance(tmp_path, allow):
    repo = scratch(tmp_path, {"notes.md": f"made by {WORD.title()}\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "rm", "-q", "notes.md")
    if allow:
        row = f"notes.md\t{hashlib.sha256(WORD.encode()).hexdigest()}\tpublic attribution\n"
        with open(repo / "scripts" / "no-person-allowance.tsv", "a", encoding="utf-8") as handle:
            handle.write(row)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: remove old content")
    code, output = scan(repo)
    if allow:
        assert code == 0, "the historical path allowance was ignored: " + output
        assert "stale allowance" not in output
    else:
        assert code == 1, "an unallowed historical blob was accepted"
        assert f"commit {planted[:12]}: notes.md:1: deny-list" in output.splitlines()
    assert WORD not in output.lower()


def test_a_historical_submodule_refuses(tmp_path):
    repo = scratch(tmp_path)
    target = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{target},vendor/component")
    git(repo, "commit", "-q", "-m", "chore: add component")
    planted = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-index", "--force-remove", "vendor/component")
    git(repo, "commit", "-q", "-m", "chore: drop component")
    code, output = scan(repo)
    assert code == 1, "a historical submodule was accepted"
    assert f"commit {planted[:12]}: vendor/component: unreadable (a submodule entry, not a file)" in output.splitlines()


def test_a_shallow_clone_refuses_without_accepting_hidden_history(tmp_path):
    repo = scratch(tmp_path, {"notes.md": f"made by {WORD.title()}\n"})
    (repo / "notes.md").write_text("clean now\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: remove old content")
    shallow = tmp_path / "shallow"
    git(repo, "clone", "-q", "--depth", "1", repo.as_uri(), str(shallow))
    assert git(shallow, "rev-parse", "--is-shallow-repository").strip() == "true"
    code, output = scan(shallow)
    assert code == 1, "a shallow clone was accepted without its hidden history"
    assert "history_shallow: full history is required" in output
    assert WORD not in output.lower()


def test_a_planted_word_in_a_commit_message_fails(tmp_path):
    repo = scratch(tmp_path)
    (repo / "b.md").write_text("b\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"fix: thanks {WORD}")
    code, output = scan(repo)
    assert code == 1
    assert ": message line 1: deny-list" in output


def test_a_planted_word_in_an_author_identity_fails(tmp_path):
    repo = scratch(tmp_path, identity=("Test", f"{WORD}@example.invalid"))
    code, output = scan(repo)
    assert code == 1
    assert ": author: deny-list" in output
    assert ": committer: deny-list" in output


def test_an_identity_allowance_row_allows_that_identity_only(tmp_path):
    repo = scratch(tmp_path, identity=("Test", f"{WORD}@example.invalid"))
    row = f":commit-identity\t{hashlib.sha256(WORD.encode()).hexdigest()}\tthe repository owner's public commit identity\n"
    with open(repo / "scripts" / "no-person-allowance.tsv", "a", encoding="utf-8") as handle:
        handle.write(row)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: allowance", identity=("Test", f"{WORD}@example.invalid"))
    code, output = scan(repo)
    assert code == 0, output
    (repo / "README.md").write_text(f"{WORD}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: x", identity=("Test", f"{WORD}@example.invalid"))
    code, output = scan(repo)
    assert code == 1
    assert "README.md:1: deny-list" in output


def test_a_stale_allowance_row_fails(tmp_path):
    repo = scratch(tmp_path)
    row = f"README.md\t{hashlib.sha256(b'nothing').hexdigest()}\tno longer needed\n"
    with open(repo / "scripts" / "no-person-allowance.tsv", "a", encoding="utf-8") as handle:
        handle.write(row)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: row")
    code, output = scan(repo)
    assert code == 1
    assert "stale allowance" in output


def test_a_missing_pattern_list_fails_in_ci(tmp_path):
    code, output = scan(scratch(tmp_path), deny=None, ci=True)
    assert code == 1
    assert "NO_PERSON_DENY is unset or empty in CI" in output
    code, output = scan(scratch(tmp_path / "b"), deny="", ci=True)
    assert code == 1


def test_a_missing_pattern_list_warns_outside_ci(tmp_path):
    code, output = scan(scratch(tmp_path), deny=None, ci=False)
    assert code == 0, output
    assert "WARN: NO_PERSON_DENY IS UNSET" in output


def test_a_pattern_list_matching_the_empty_string_fails(tmp_path):
    code, output = scan(scratch(tmp_path), deny="x|")
    assert code == 1
    assert "matches the empty string" in output


def test_a_missing_allowance_file_fails(tmp_path):
    repo = scratch(tmp_path)
    git(repo, "rm", "-q", "scripts/no-person-allowance.tsv")
    git(repo, "commit", "-q", "-m", "chore: drop")
    code, output = scan(repo)
    assert code == 1
    assert "unreadable (not tracked as a regular file)" in output


def test_the_license_line_is_carved_for_the_pinned_holder_only(tmp_path):
    holder = "Andr" + "ew Smi" + "th"
    deny = holder.split()[0].lower()
    code, output = scan(scratch(tmp_path, {"LICENSE": f"Copyright (c) 2026 {holder}\n"}), deny=deny)
    assert code == 0, output
    code, output = scan(scratch(tmp_path / "b", {"LICENSE": f"Copyright (c) 2026 {holder} Jr\n"}), deny=deny)
    assert code == 1


def test_the_workflow_runs_the_scan_on_pull_requests_and_pushes_with_its_secret():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = doc[True]
    assert "pull_request" in on and "push" in on
    assert on["push"] == {"branches": ["main"]}
    for trigger in on.values():
        assert "paths" not in (trigger or {}) and "paths-ignore" not in (trigger or {})
    scan_jobs = [job for job in doc["jobs"].values()
                 if any("check-no-person.py" in str(step.get("run", "")) for step in job["steps"])]
    assert len(scan_jobs) == 1
    (scan_job,) = scan_jobs
    checkout = scan_job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    env = {}
    for step in scan_job["steps"]:
        env.update(step.get("env", {}))
    assert env["NO_PERSON_DENY"] == "${{ secrets.NO_PERSON_DENY }}"


def test_every_job_requires_public_repository_visibility():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for name, job in doc["jobs"].items():
        assert job.get("if") == "${{ !github.event.repository.private }}", name


@pytest.mark.parametrize("path", ["pyproject.toml"])
def test_pyyaml_is_a_declared_dependency(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert 'dependencies = ["PyYAML>=6"]' in text
