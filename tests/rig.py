"""The test rig: a fake ``gh`` at the process boundary, a scenario of one repository, a test records port.

The fake ``gh`` is an executable script. It appends every argv it is called
with to a log, and answers from a routes file keyed by the exact argv; an
argv with no route exits 1. Repeated calls of one argv take the route's
responses in order, then repeat the last. Nothing answers at the Python
function level, so every request the gate makes is exercised as it is sent.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from review_gate import gate
from review_gate.gh import GhTransport
from review_gate.github import COMPARE_FILES_JQ, MERGED_QUERY, STATE_QUERY
from review_gate.records import DatabasePaths
from review_gate.store import StoreCheck

SEP = "\x1f"
ITEM = "it-0123456789ab"
READY_AT = "2026-11-20T10:00:00Z"
AFTER_READY = "2026-11-20T10:05:00Z"
LATER = "2026-11-20T10:09:00Z"
BEFORE_READY = "2026-11-20T09:55:00Z"

CI_YML = """name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""

LINT_DOCS_YML = """name: Docs lint
on:
  push:
    branches: [main]
  pull_request:
    paths: ["docs/**"]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""

FAKE_GH = """#!{python}
import json, os, sys
ROUTES = {routes!r}
LOG = {log!r}
COUNTS = {counts!r}
argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
with open(ROUTES, encoding="utf-8") as handle:
    routes = json.load(handle)
key = "\\x1f".join(argv)
if key not in routes:
    sys.stderr.write("fake gh: no route for " + " ".join(argv) + "\\n")
    sys.exit(1)
counts = {{}}
if os.path.exists(COUNTS):
    with open(COUNTS, encoding="utf-8") as handle:
        counts = json.load(handle)
seen = counts.get(key, 0)
counts[key] = seen + 1
with open(COUNTS, "w", encoding="utf-8") as handle:
    json.dump(counts, handle)
responses = routes[key]
answer = responses[min(seen, len(responses) - 1)]
sys.stdout.write(answer.get("stdout", ""))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("exit", 0))
"""


def oid(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()


def key(argv) -> str:
    return SEP.join(argv)


class FakeGh:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.routes_path = directory / "routes.json"
        self.log_path = directory / "calls.jsonl"
        self.counts_path = directory / "counts.json"
        self.path = directory / "gh"
        self.path.write_text(
            FAKE_GH.format(
                python=sys.executable,
                routes=str(self.routes_path),
                log=str(self.log_path),
                counts=str(self.counts_path),
            ),
            encoding="utf-8",
        )
        self.path.chmod(0o755)
        self.routes = {}
        self.save()

    def save(self) -> None:
        self.routes_path.write_text(json.dumps(self.routes), encoding="utf-8")

    def set_routes(self, routes: dict) -> None:
        self.routes = routes
        self.save()

    def calls(self) -> list:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def transport(self) -> GhTransport:
        return GhTransport(str(self.path))


def ok(doc) -> dict:
    return {"stdout": doc if isinstance(doc, str) else json.dumps(doc)}


def fail(code: int = 1, stderr: str = "HTTP 502") -> dict:
    return {"stdout": "", "stderr": stderr + "\n", "exit": code}


def run_doc(run_id: int, path: str, *, event="pull_request", head_sha, head_branch="feature",
            created=AFTER_READY, status="completed", conclusion="success", repo="octo/widget",
            head_repo="octo/widget") -> dict:
    return {
        "id": run_id,
        "path": path,
        "name": path,
        "event": event,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "head_repository": {"full_name": head_repo},
        "repository": {"full_name": repo},
        "created_at": created,
        "status": status,
        "conclusion": conclusion,
        "pull_requests": [],
    }


def job(name: str, conclusion="success", steps=None) -> dict:
    return {"name": name, "conclusion": conclusion, "steps": steps or []}


def proof_jobs(sha: str, skipped: str = "long tests") -> list:
    """A push run's jobs with one skipped job and a valid main tree proof."""
    return [
        job("test"),
        job(skipped, "skipped"),
        job("main tree proof", steps=[{"name": f"Main tree proof: head {sha} run 1234", "conclusion": "success"}]),
    ]


class Repo:
    """One repository, one pull request and every GitHub answer the gate reads, green by default."""

    owner = "octo"
    name = "widget"
    number = 7

    def __init__(self) -> None:
        self.head = oid("head")
        self.base_oid = oid("base")
        self.tip = self.base_oid
        self.default = "main"
        self.head_branch = "feature"
        self.files = [["src/app.py", None]]
        self.files_read2 = None
        self.head_workflows = {"ci.yml": CI_YML}
        self.tip_workflows = {"ci.yml": CI_YML}
        self.timeline = [{"event": "ready_for_review", "created_at": READY_AT}]
        self.pull_record = {"number": self.number, "draft": False, "created_at": BEFORE_READY,
                            "body": "", "state": "open"}
        self.pr_runs = [run_doc(101, ".github/workflows/ci.yml", head_sha=self.head)]
        self.jobs = {101: [job("test")]}
        self.push_runs = {"ci.yml": [run_doc(201, ".github/workflows/ci.yml", event="push",
                                             head_sha=self.tip, head_branch="main")]}
        self.jobs[201] = [job("test")]
        self.branch_pulls = [{"number": self.number, "state": "open", "head": {"ref": "feature"}}]
        self.state1 = {}
        self.state2 = {}
        self.base_compare = [{"status": "ahead", "behind_by": 0, "ahead_by": 1, "base_commit": {"sha": None}}]
        self.merge_answer = {"stdout": "", "exit": 0}
        self.merged = {"state": "MERGED", "merged": True, "headRefOid": None, "mergeCommit": {"oid": oid("merge")}}
        self.extra = {}

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}/pull/{self.number}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    def argv(self, *extra, items=ITEM) -> list:
        return ["merge", self.url, self.head, "--items", items, *extra]

    # ---- documents ----

    def state_doc(self, overrides: dict) -> dict:
        pull = {
            "number": self.number,
            "headRefOid": self.head,
            "headRefName": self.head_branch,
            "headRepository": {"nameWithOwner": self.slug},
            "isCrossRepository": False,
            "baseRefName": self.default,
            "baseRefOid": self.base_oid,
            "state": "OPEN",
            "isDraft": False,
            "changedFiles": len(self.files),
        }
        repository = {"nameWithOwner": self.slug, "defaultBranchRef": {"name": self.default}, "pullRequest": pull}
        doc = {"data": {"repository": repository}}
        for field, value in overrides.items():
            if field == "__doc__":
                doc = value
            elif field == "nameWithOwner":
                repository["nameWithOwner"] = value
            elif field == "default":
                repository["defaultBranchRef"] = {"name": value}
            elif field == "headRepository":
                pull["headRepository"] = None if value is None else {"nameWithOwner": value}
            else:
                pull[field] = value
        return doc

    def graphql_argv(self, query: str) -> list:
        return ["api", "graphql", "-f", f"query={query}", "-f", f"owner={self.owner}", "-f",
                f"name={self.name}", "-F", f"number={self.number}"]

    def files_text(self, files) -> str:
        return "".join(json.dumps(pair) + "\n" for pair in files)

    def tree_routes(self, routes: dict, commit: str, workflows: dict | None) -> None:
        label = f"tree-{commit}-" + json.dumps(workflows, sort_keys=True)
        root = oid(label + "root")
        root_entries = [{"path": "src", "type": "tree", "mode": "040000", "sha": oid(label + "src")}]
        routes[key(["api", f"repos/{self.slug}/git/commits/{commit}"])] = [ok({"sha": commit, "tree": {"sha": root}})]
        if workflows is not None:
            github_tree = oid(label + ".github")
            wf_tree = oid(label + "workflows")
            root_entries.append({"path": ".github", "type": "tree", "mode": "040000", "sha": github_tree})
            routes[key(["api", f"repos/{self.slug}/git/trees/{github_tree}"])] = [ok({
                "sha": github_tree, "truncated": False,
                "tree": [{"path": "workflows", "type": "tree", "mode": "040000", "sha": wf_tree}],
            })]
            entries = []
            for name, text in sorted(workflows.items()):
                blob = oid("blob" + text)
                entries.append({"path": name, "type": "blob", "mode": "100644", "sha": blob})
                routes[key(["api", f"repos/{self.slug}/git/blobs/{blob}"])] = [ok({
                    "sha": blob, "encoding": "base64",
                    "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                })]
            routes[key(["api", f"repos/{self.slug}/git/trees/{wf_tree}"])] = [ok({
                "sha": wf_tree, "truncated": False, "tree": entries,
            })]
        routes[key(["api", f"repos/{self.slug}/git/trees/{root}"])] = [ok({
            "sha": root, "truncated": False, "tree": root_entries,
        })]

    def routes(self) -> dict:
        routes = {}
        routes[key(self.graphql_argv(STATE_QUERY))] = [
            ok(self.state_doc(self.state1)), ok(self.state_doc({**self.state1, **self.state2}))
        ]
        compare = ["api", "--jq", COMPARE_FILES_JQ, f"repos/{self.slug}/compare/{self.base_oid}...{self.head}"]
        second = self.files if self.files_read2 is None else self.files_read2
        routes[key(compare)] = [ok(self.files_text(self.files)), ok(self.files_text(second))]
        self.tree_routes(routes, self.head, self.head_workflows or None)
        self.tree_routes(routes, self.tip, self.tip_workflows or None)
        routes[key(["api", "--paginate", "--slurp",
                    f"repos/{self.slug}/issues/{self.number}/timeline?per_page=100"])] = [ok([self.timeline])]
        routes[key(["api", f"repos/{self.slug}/pulls/{self.number}"])] = [ok(self.pull_record)]
        routes[key(["api", "--paginate", "--slurp",
                    f"repos/{self.slug}/actions/runs?head_sha={self.head}&per_page=100"])] = [
            ok([{"total_count": len(self.pr_runs), "workflow_runs": self.pr_runs}])
        ]
        for run_id, jobs in self.jobs.items():
            routes[key(["api", "--paginate", "--slurp",
                        f"repos/{self.slug}/actions/runs/{run_id}/jobs?per_page=100"])] = [ok([{"jobs": jobs}])]
        routes[key(["api", "--paginate", "--slurp",
                    f"repos/{self.slug}/pulls?head={self.owner}%3A{self.head_branch}&state=all&per_page=100"])] = [
            ok([self.branch_pulls])
        ]
        routes[key(["api", f"repos/{self.slug}/git/ref/heads/{self.default}"])] = [
            ok({"ref": f"refs/heads/{self.default}", "object": {"type": "commit", "sha": self.tip}})
        ]
        for name, runs in self.push_runs.items():
            routes[key(["api", f"repos/{self.slug}/actions/workflows/{name}/runs"
                               f"?branch={self.default}&event=push&per_page=20"])] = [ok({"workflow_runs": runs})]
        compares = []
        for doc in self.base_compare:
            doc = copy.deepcopy(doc)
            commit = doc.get("base_commit") if isinstance(doc, dict) else None
            if isinstance(commit, dict) and "sha" in commit and commit["sha"] is None:
                doc["base_commit"]["sha"] = self.tip
            compares.append(ok(doc) if not isinstance(doc, dict) or "exit" not in doc else doc)
        routes[key(["api", f"repos/{self.slug}/compare/{self.default}...{self.head}"])] = compares
        routes[key(["pr", "merge", self.url, "--squash", "--match-head-commit", self.head])] = [self.merge_answer]
        merged = copy.deepcopy(self.merged)
        if merged.get("headRefOid") is None:
            merged["headRefOid"] = self.head
        routes[key(self.graphql_argv(MERGED_QUERY))] = [ok({"data": {"repository": {"pullRequest": merged}}})]
        routes.update(self.extra)
        return routes


@dataclass
class StandingGrant:
    grant_id: str
    status: str
    deciding_event_id: int


class LedgerRefusal(Exception):
    """The shape of the ledger's CliError: an exception with a ``code``."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class TestPort:
    """A records port filled from Python, recording every call."""

    __test__ = False

    def __init__(self, paths: DatabasePaths) -> None:
        self.paths = paths
        self.calls = []
        self.subjects = []
        self.grants = {}
        self.item_answers = [ITEM]
        self.token_answers = {stage: ["ok"] for stage in ("receipts", "open_bugs", "hardening", "round_cap")}
        self.state = "absent"
        self.intent_error = None
        self.finished_error = None
        self.seen = {}

    def _next(self, name: str, answers: list):
        seen = self.seen.get(name, 0)
        self.seen[name] = seen + 1
        return answers[min(seen, len(answers) - 1)]

    def database_paths(self):
        self.calls.append(("database_paths",))
        return self.paths

    def item(self, subject):
        self.calls.append(("item", subject.url))
        self.subjects.append(subject)
        return self._next("item", self.item_answers)

    def _token(self, stage, subject, item):
        self.calls.append((stage, item))
        return self._next(stage, self.token_answers[stage])

    def receipts(self, subject, item):
        return self._token("receipts", subject, item)

    def open_bugs(self, subject, item):
        return self._token("open_bugs", subject, item)

    def hardening(self, subject, item):
        return self._token("hardening", subject, item)

    def round_cap(self, subject, item):
        return self._token("round_cap", subject, item)

    def standing(self, grant_id):
        self.calls.append(("standing", grant_id))
        answer = self.grants.get(grant_id)
        if answer is None:
            raise LedgerRefusal(f"grant {grant_id!r} is not recorded", "grant_unknown")
        if isinstance(answer, list):
            answer = self._next(grant_id, answer)
        if isinstance(answer, str):
            raise LedgerRefusal(f"grant {grant_id!r} refused", answer)
        return StandingGrant(grant_id, "STANDING", answer)

    def merge_state(self, item, merge_key):
        self.calls.append(("merge_state", item, merge_key))
        return self.state

    def write_intent(self, item, merge_key):
        self.calls.append(("write_intent", item, merge_key))
        if self.intent_error is not None:
            raise self.intent_error

    def write_finished(self, item, merge_key, merge_commit):
        self.calls.append(("write_finished", item, merge_key, merge_commit))
        if self.finished_error is not None:
            raise self.finished_error

    def close_items(self, items, url, head):
        self.calls.append(("close_items", items, url, head))

    def names(self) -> list:
        return [call[0] for call in self.calls]


def write_roots(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# test roots\n" + "".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


@dataclass
class Store:
    root: Path
    paths: DatabasePaths
    check: StoreCheck
    lane_tmp: Path
    user_home: Path
    temp_roots: Path
    sandbox_roots: Path


def make_store(tmp_path: Path, name: str = "ledger-home") -> Store:
    """A ledger database in its own directory, outside every root the test check lists."""
    tmp_path = Path(os.path.realpath(tmp_path))
    root = tmp_path / name
    root.mkdir(mode=0o700)
    database = root / "ledger.db"
    database.write_bytes(b"")
    database.chmod(0o600)
    lane_tmp = tmp_path / "lane-tmp"
    lane_tmp.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    temp_roots = write_roots(tmp_path / "roots" / "temp-roots.txt", [str(lane_tmp)])
    sandbox_roots = write_roots(tmp_path / "roots" / "sandbox-roots.txt", [".npm/_logs", ".claude/debug"])
    check = StoreCheck(
        str(temp_roots), str(sandbox_roots),
        environ={"TMPDIR": str(lane_tmp), "HOME": str(home)},
        gettempdir=lambda: str(lane_tmp),
    )
    paths = DatabasePaths(str(database), str(database) + ".documents")
    return Store(root, paths, check, lane_tmp, home, temp_roots, sandbox_roots)


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def reason(self) -> str:
        line = self.err.strip().splitlines()[0] if self.err.strip() else ""
        return line.split(":", 1)[0]


class Rig:
    """A repository scenario, a fake gh, a test port and a store, run together."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.repo = Repo()
        self.fake = FakeGh(tmp_path / "fakegh")
        self.store = make_store(tmp_path)
        self.port = TestPort(self.store.paths)

    def run(self, *extra, argv=None) -> Result:
        self.fake.set_routes(self.repo.routes())
        out, err = io.StringIO(), io.StringIO()
        code = gate.run(
            self.repo.argv(*extra) if argv is None else argv,
            records=self.port,
            gh=self.fake.transport(),
            store_check=self.store.check,
            out=out,
            err=err,
        )
        return Result(code, out.getvalue(), err.getvalue())

    def merge_calls(self) -> list:
        return [call for call in self.fake.calls() if call[:2] == ["pr", "merge"]]

    def endpoints(self) -> list:
        """Each gh call as one string, in order."""
        return [" ".join(call) for call in self.fake.calls()]


def mode_without_group_write(path: Path) -> None:
    path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
