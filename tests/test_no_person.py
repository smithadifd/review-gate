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
    assert "README.md:1: deny-list" in output
    assert WORD not in output.lower()


def test_a_word_removed_from_the_tree_still_fails_in_history(tmp_path):
    repo = scratch(tmp_path, {"notes.md": f"made by {WORD.title()}\n"})
    planted = git(repo, "rev-parse", "HEAD").strip()
    (repo / "notes.md").write_text("clean now\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs: remove old content")
    code, output = scan(repo)
    assert code == 1, "reachable historical blob was accepted"
    assert f"commit {planted[:12]}: notes.md:1: deny-list" in output
    assert WORD not in output.lower()


def test_a_planted_home_path_fails(tmp_path):
    code, output = scan(scratch(tmp_path, {"notes.md": "see /Us" + "ers/someone/x\n"}))
    assert code == 1
    assert "notes.md:1: no-person shape: absolute home" in output


def test_a_planted_word_in_a_path_name_fails(tmp_path):
    code, output = scan(scratch(tmp_path, {f"docs/{WORD}.md": "x\n"}))
    assert code == 1
    assert "deny-list (path name)" in output


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


def test_every_job_stays_off_while_the_repository_is_private():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for name, job in doc["jobs"].items():
        assert job.get("if") == "${{ !github.event.repository.private }}", name


@pytest.mark.parametrize("path", ["pyproject.toml"])
def test_pyyaml_is_a_declared_dependency(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert 'dependencies = ["PyYAML>=6"]' in text
