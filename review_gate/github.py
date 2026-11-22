"""The GitHub reads the gate decides on, each checked for its shape.

State (stages 2 and 10). ``STATE_QUERY`` is one GraphQL read of the
repository's name and default branch and the pull request's number, head
OID, head branch, head repository, cross-repository flag, base name, base
OID, state, draft flag and changedFiles. It has no ``body`` field: no stage
reads the pull request body. ``state_gate`` then requires, in this order,
the head to equal the sha (``head_mismatch``), the pull request to be open
(``pr_not_open``), not a draft (``pr_is_draft``), based on the default
branch (``pr_base_not_default``), and its head branch to live in the base
repository (``pr_head_fork``); the repository must be the one the URL names
(``pr_repository_mismatch``). A response with ``errors``, a null repository
or pull request, or a field of the wrong type refuses ``pr_view_unparseable``.

Files. The file list is the comparison of the first state read's base OID
and head OID, read with ``COMPARE_FILES_JQ`` (each file's name and previous
name). A list at GitHub's 300-file cap refuses ``compare_files_capped``; one
that does not list changedFiles files refuses ``pr_files_count_mismatch``.
``files_digest`` is the sha256 of the sorted, de-duplicated paths (each
rename's previous and new name), each NUL-terminated: the review runner's
digest, so a receipt's recorded digest compares as it is.

Base in head (stage 10). ``compare/<default>...<sha>`` must read
``behind_by`` 0 (``base_not_in_head``); a failed read refuses
``base_compare_failed`` and an unexpected shape ``base_compare_unparseable``.
It returns the tip the comparison was made against (``base_commit.sha``).

READY. The latest ``ready_for_review`` timeline event, unless a
``convert_to_draft`` event is at or after it (refused); with neither event,
the creation time, only when two timeline reads bracket a read of the pull
request's record showing it non-draft. The REST record carries a body; only
its number, draft flag and creation time are read.

Workflow files are read from the git objects of an exact commit sha
(``git/commits``, then ``git/trees``, then ``git/blobs``), never from a
branch name.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone

from .gh import api_json, api_pages, call, graphql
from .refusal import refuse

OID = re.compile(r"[0-9a-f]{40}")
COMPARE_FILES_CAP = 300
COMPARE_FILES_JQ = ".files[] | [.filename, .previous_filename] | tojson"
STATE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{nameWithOwner defaultBranchRef{name} pullRequest(number:$number){number headRefOid headRefName "
    "headRepository{nameWithOwner} isCrossRepository baseRefName baseRefOid state isDraft changedFiles}}}"
)
MERGED_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
    "{pullRequest(number:$number){state merged headRefOid mergeCommit{oid}}}}"
)
# Read 2 must equal read 1 on every one of these, each refused by its own name.
STATE_FIELDS = (
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
STATUSES_NOT_BEHIND = ("ahead", "identical")
STATUSES_BEHIND = ("behind", "diverged")
# What git allows in a branch name, less what would change an endpoint's path.
BRANCH = re.compile(r"[^\x00-\x20\x7f~^:?*\[\\]+")
WORKFLOW_DIRECTORY = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
REGULAR_MODES = ("100644", "100755")


def is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _plain_name(value) -> bool:
    return isinstance(value, str) and value != "" and not any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in value
    )


# ---- state ---------------------------------------------------------------

def parse_state(doc) -> dict:
    """The fields of one STATE_QUERY response, each of its type, or ``pr_view_unparseable``."""
    if not isinstance(doc, dict) or "errors" in doc:
        refuse("pr_view_unparseable", "errors" if isinstance(doc, dict) else "not an object")
    try:
        repository = doc["data"]["repository"]
        if repository is None:
            refuse("pr_view_unparseable", "repository is null")
        pull = repository["pullRequest"]
        if pull is None:
            refuse("pr_view_unparseable", "pullRequest is null")
        head_repository = pull["headRepository"]
        state = {
            "repository": repository["nameWithOwner"],
            "default": repository["defaultBranchRef"]["name"],
            "number": pull["number"],
            "headRefOid": pull["headRefOid"],
            "headRefName": pull["headRefName"],
            "headRepository": None if head_repository is None else head_repository["nameWithOwner"],
            "isCrossRepository": pull["isCrossRepository"],
            "baseRefName": pull["baseRefName"],
            "baseRefOid": pull["baseRefOid"],
            "state": pull["state"],
            "isDraft": pull["isDraft"],
            "changedFiles": pull["changedFiles"],
        }
    except (KeyError, TypeError) as missing:
        refuse("pr_view_unparseable", f"missing {missing}")
    kinds = {
        "repository": lambda v: isinstance(v, str) and "/" in v,
        "default": _plain_name,
        "number": lambda v: is_count(v) and v > 0,
        "headRefOid": lambda v: isinstance(v, str) and OID.fullmatch(v) is not None,
        "headRefName": _plain_name,
        "headRepository": lambda v: v is None or (isinstance(v, str) and "/" in v),
        "isCrossRepository": lambda v: isinstance(v, bool),
        "baseRefName": _plain_name,
        "baseRefOid": lambda v: isinstance(v, str) and OID.fullmatch(v) is not None,
        "state": lambda v: isinstance(v, str),
        "isDraft": lambda v: isinstance(v, bool),
        "changedFiles": is_count,
    }
    for field, valid in kinds.items():
        if not valid(state[field]):
            refuse("pr_view_unparseable", field)
    return state


def read_state(gh, pr) -> dict:
    doc = graphql(gh, STATE_QUERY, pr.owner, pr.repo, pr.number, "pr_view_failed")
    return parse_state(doc)


def state_gate(state: dict, pr, sha: str) -> None:
    """The pull request is the URL's, at ``sha``, open, ready, on the default branch and not a fork."""
    if state["headRefOid"] != sha:
        refuse("head_mismatch", f"pr={state['headRefOid']} given={sha}")
    if state["repository"].casefold() != pr.key or state["number"] != pr.number:
        refuse(
            "pr_repository_mismatch",
            f"GitHub answered for {state['repository']}#{state['number']}, not {pr.slug}#{pr.number}",
        )
    if state["state"] != "OPEN":
        refuse("pr_not_open", f"state={state['state']}")
    if state["isDraft"] is not False:
        refuse("pr_is_draft")
    if state["baseRefName"] != state["default"]:
        refuse(
            "pr_base_not_default",
            f"baseRefName={state['baseRefName']}, the repository's default branch is {state['default']}",
        )
    head_repository = state["headRepository"]
    if (
        state["isCrossRepository"] is not False
        or head_repository is None
        or head_repository.casefold() != state["repository"].casefold()
    ):
        refuse("pr_head_fork", f"the head branch lives in {head_repository!r}, not {state['repository']}")


def compare_state(first: dict, final: dict) -> None:
    """Read 2 equals read 1 on every STATE_FIELDS field, each refused by its own name."""
    for field, name in STATE_FIELDS:
        if first[field] != final[field]:
            refuse(name, f"{field} {first[field]!r} -> {final[field]!r}")


# ---- files ---------------------------------------------------------------

def path_problem(path) -> str | None:
    """Why ``path`` is not a repo-relative POSIX path, or None; paths are never normalized."""
    if not isinstance(path, str):
        return "not a string"
    if not path:
        return "empty"
    if path.startswith("/"):
        return "absolute"
    if "\0" in path:
        return "contains NUL"
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            return f"has a {segment!r} segment"
    try:
        path.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return "not encodable"
    return None


def files_digest(paths) -> str:
    """The sha256 of the sorted, de-duplicated paths, each NUL-terminated."""
    return hashlib.sha256(
        b"".join(path.encode("utf-8", "surrogateescape") + b"\0" for path in sorted(dict.fromkeys(paths)))
    ).hexdigest()


def compare_file_paths(data: bytes, changed_files) -> list:
    """The paths of one COMPARE_FILES_JQ output: each previous name, then its name, first occurrence only."""
    if not is_count(changed_files):
        refuse("pr_files_unresolved", f"changedFiles={changed_files!r} is not a file count")
    entries_count = sum(1 for line in data.split(b"\n") if line)
    if entries_count >= COMPARE_FILES_CAP:
        refuse(
            "compare_files_capped",
            f"the comparison lists {entries_count} files, its cap of {COMPARE_FILES_CAP}; the list may be truncated",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        refuse("pr_files_unresolved", "the file list is not UTF-8")
    entries = []
    first = {}
    for number, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        try:
            pair = json.loads(line)
        except ValueError:
            refuse("pr_files_unresolved", f"line {number} is not JSON")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not (pair[1] is None or isinstance(pair[1], str))
        ):
            refuse("pr_files_unresolved", f"line {number} is not [filename, previous_filename]")
        filename, previous = pair
        for path in (filename,) if previous is None else (previous, filename):
            problem = path_problem(path)
            if problem is not None:
                refuse("pr_files_unresolved", f"line {number} path {path!r} ({problem})")
        if filename in first:
            refuse("pr_files_unresolved", f"line {number} repeats filename {filename} (first on line {first[filename]})")
        first[filename] = number
        entries.append((filename, previous))
    if len(entries) != changed_files:
        refuse(
            "pr_files_count_mismatch",
            f"gh listed {len(entries)} file(s), the pull request reports changedFiles={changed_files}",
        )
    if not entries:
        refuse("pr_files_unresolved", "the pull request lists no changed file")
    paths = []
    for filename, previous in entries:
        if previous is not None:
            paths.append(previous)
        paths.append(filename)
    return list(dict.fromkeys(paths))


def compare_endpoint(pr, state: dict) -> str:
    return f"repos/{pr.owner}/{pr.repo}/compare/{state['baseRefOid']}...{state['headRefOid']}"


def read_files(gh, pr, state: dict) -> list:
    endpoint = compare_endpoint(pr, state)
    raw = call(gh, ["api", "--jq", COMPARE_FILES_JQ, endpoint], "pr_files_unresolved", f"gh api {endpoint}")
    return compare_file_paths(raw, state["changedFiles"])


# ---- base in head --------------------------------------------------------

def _quoted_branch(branch: str, name: str) -> str:
    if not BRANCH.fullmatch(branch) or ".." in branch or branch.startswith("/") or branch.endswith("/"):
        refuse(name, f"branch name {branch!r} cannot name an endpoint")
    return urllib.parse.quote(branch, safe="/")


def _count_field(doc: dict, key: str) -> int:
    value = doc.get(key)
    if not is_count(value):
        refuse("base_compare_unparseable", f"{key} is {json.dumps(value)}, not a count")
    return value


def base_in_head(gh, pr, sha: str, base: str) -> str:
    """The base branch tip the comparison read, when it is in ``sha``; otherwise a refusal."""
    endpoint = f"repos/{pr.owner}/{pr.repo}/compare/{_quoted_branch(base, 'base_compare_failed')}...{sha}"
    doc = api_json(gh, endpoint, "base_compare_failed")
    if not isinstance(doc, dict):
        refuse("base_compare_unparseable", "the response is not an object")
    behind = _count_field(doc, "behind_by")
    _count_field(doc, "ahead_by")
    status = doc.get("status")
    if status not in STATUSES_NOT_BEHIND + STATUSES_BEHIND:
        refuse("base_compare_unparseable", f"status is {json.dumps(status)}")
    if (behind == 0) != (status in STATUSES_NOT_BEHIND):
        refuse("base_compare_unparseable", f"status {status} with behind_by {behind}")
    tip = doc.get("base_commit")
    tip = tip.get("sha") if isinstance(tip, dict) else None
    if not isinstance(tip, str) or not OID.fullmatch(tip):
        refuse("base_compare_unparseable", "base_commit.sha is missing")
    if behind:
        refuse(
            "base_not_in_head",
            f"the head {sha} is {behind} commit(s) behind {base} at {tip} (status {status}); update the branch",
        )
    return tip


def import_base(gh, pr, head, branch):
    """The compare merge base at import time, not the review's original base."""
    endpoint = f"repos/{pr.owner}/{pr.repo}/compare/{_quoted_branch(branch, 'import_base_unresolved')}...{head}"
    doc = api_json(gh, endpoint, "import_base_unresolved")
    commit = doc.get("merge_base_commit") if isinstance(doc, dict) else None
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or OID.fullmatch(sha) is None:
        refuse("import_base_unresolved", "compare has no merge_base_commit.sha")
    return sha


# ---- times, READY and the head branch -------------------------------------

def parse_time(value, field: str, name: str) -> datetime:
    if not isinstance(value, str):
        refuse(name, f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refuse(name, f"{field} is not an ISO timestamp: {value!r}")
    if parsed.tzinfo is None:
        refuse(name, f"{field} has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _transition_ready_time(gh, pr) -> datetime | None:
    pages = api_pages(gh, f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/timeline?per_page=100", "ci_read_failed")
    ready = []
    converted = []
    for page in pages:
        if not isinstance(page, list):
            refuse("ci_read_failed", "timeline page is not a list")
        for event in page:
            if not isinstance(event, dict):
                continue
            kind = event.get("event")
            if kind == "ready_for_review":
                text = event.get("created_at")
                ready.append((parse_time(text, "ready_for_review.created_at", "ci_ready_unresolved"), text))
            elif kind == "convert_to_draft":
                converted.append(event.get("created_at"))
    if ready:
        ready_at, ready_text = max(ready, key=lambda pair: pair[0])
        if converted:
            converted_at, converted_text = max(
                ((parse_time(text, "convert_to_draft.created_at", "ci_ready_unresolved"), text) for text in converted),
                key=lambda pair: pair[0],
            )
            if converted_at.replace(microsecond=0) >= ready_at.replace(microsecond=0):
                refuse(
                    "ci_ready_unresolved",
                    f"the pull request was converted to draft at {converted_text}, not before its newest "
                    f"ready_for_review at {ready_text}",
                )
        return ready_at
    if converted:
        refuse("ci_ready_unresolved", "the pull request was converted to draft and has no ready_for_review event")
    return None


def ready_time(gh, pr) -> datetime:
    """The READY time post-READY runs count from, or a refusal."""
    ready_at = _transition_ready_time(gh, pr)
    if ready_at is not None:
        return ready_at
    endpoint = f"repos/{pr.owner}/{pr.repo}/pulls/{pr.number}"
    pull = api_json(gh, endpoint, "ci_read_failed")
    if not isinstance(pull, dict) or pull.get("number") != pr.number:
        refuse("ci_read_failed", f"gh api {endpoint} did not return pull request {pr.number}")
    draft = pull.get("draft")
    if not isinstance(draft, bool):
        refuse("ci_read_failed", "the pull request's draft flag is missing")
    if draft is not False:
        refuse("ci_ready_unresolved", "the pull request was opened as a draft and has no ready_for_review event")
    created_at = parse_time(pull.get("created_at"), "pull_request.created_at", "ci_read_failed")
    ready_at = _transition_ready_time(gh, pr)
    if ready_at is not None:
        return ready_at
    return created_at


def head_branch_problem(gh, pr, branch: str, ready: datetime) -> str | None:
    """Why ``branch`` is not this pull request's alone, or None."""
    head = f"{pr.owner}:{branch}"
    endpoint = (
        f"repos/{pr.owner}/{pr.repo}/pulls?head={urllib.parse.quote(head, safe='')}&state=all&per_page=100"
    )
    listed_self = False
    for page in api_pages(gh, endpoint, "ci_read_failed"):
        if not isinstance(page, list):
            return f"pull request list for head {head} is malformed"
        for pull in page:
            if not isinstance(pull, dict) or not is_count(pull.get("number")):
                return f"pull request list for head {head} has a malformed entry"
            other = pull["number"]
            if other == pr.number:
                head_ref = pull.get("head")
                if isinstance(head_ref, dict) and head_ref.get("ref") == branch:
                    listed_self = True
                continue
            state = pull.get("state")
            if state == "open":
                return f"pull request #{other} is also open on head branch {branch}"
            if state != "closed":
                return f"pull request #{other} on head branch {branch} has state {state!r}"
            closed = parse_time(pull.get("closed_at"), f"pull request #{other} closed_at", "ci_read_failed")
            if closed >= ready:
                return f"pull request #{other} on head branch {branch} closed after READY ({pull.get('closed_at')})"
    if not listed_self:
        return f"pull request list for head {head} does not list #{pr.number}"
    return None


def run_jobs(gh, pr, run_id: int, name: str) -> list:
    """Every job of one run, read through every page; a malformed page or job refuses ``name``."""
    jobs = []
    for page in api_pages(gh, f"repos/{pr.owner}/{pr.repo}/actions/runs/{run_id}/jobs?per_page=100", name):
        if not isinstance(page, dict) or not isinstance(page.get("jobs"), list):
            refuse(name, f"workflow run {run_id} jobs page is malformed")
        jobs.extend(page["jobs"])
    for job in jobs:
        if not isinstance(job, dict):
            refuse(name, f"workflow run {run_id} has a malformed job")
    return jobs


def same_repository(value, slug: str) -> bool:
    """Whether a run's repository object names ``slug`` (case-insensitive, as GitHub names it)."""
    full_name = value.get("full_name") if isinstance(value, dict) else None
    return isinstance(full_name, str) and full_name.casefold() == slug.casefold()


# ---- workflow files at an exact commit -----------------------------------

def _tree_entries(gh, pr, tree_sha: str, where: str) -> list:
    doc = api_json(gh, f"repos/{pr.owner}/{pr.repo}/git/trees/{tree_sha}", "workflow_files_unreadable")
    if not isinstance(doc, dict) or doc.get("sha") != tree_sha or not isinstance(doc.get("tree"), list):
        refuse("workflow_files_unreadable", f"the tree of {where} is malformed")
    if doc.get("truncated") is not False:
        refuse("workflow_files_unreadable", f"the tree of {where} is truncated or unflagged")
    entries = []
    for entry in doc["tree"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("type"), str)
            or not isinstance(entry.get("mode"), str)
            or not isinstance(entry.get("sha"), str)
            or OID.fullmatch(entry["sha"]) is None
        ):
            refuse("workflow_files_unreadable", f"the tree of {where} has a malformed entry")
        entries.append(entry)
    return entries


def _one(entries: list, path: str, where: str):
    found = [entry for entry in entries if entry["path"] == path]
    if len(found) > 1:
        refuse("workflow_files_unreadable", f"{where} lists {path} twice")
    return found[0] if found else None


def workflow_files(gh, pr, commit: str) -> dict:
    """``{path: blob sha}`` of every ``*.yml``/``*.yaml`` file directly under .github/workflows at ``commit``.

    Empty when ``.github`` or ``.github/workflows`` is absent at the commit,
    or when ``.github`` is not a directory. A ``.github/workflows`` that is
    not a directory, or a workflow path that is not a regular file, refuses
    ``workflow_paths_unreadable``; a tree that cannot be read refuses
    ``workflow_files_unreadable``.
    """
    doc = api_json(gh, f"repos/{pr.owner}/{pr.repo}/git/commits/{commit}", "workflow_files_unreadable")
    tree = doc.get("tree") if isinstance(doc, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(doc, dict) or doc.get("sha") != commit or not isinstance(tree_sha, str) or not OID.fullmatch(tree_sha):
        refuse("workflow_files_unreadable", f"the commit {commit} does not read as a commit with a tree")
    github_dir = _one(_tree_entries(gh, pr, tree_sha, commit), ".github", commit)
    if github_dir is None or github_dir["type"] != "tree":
        return {}
    workflows = _one(_tree_entries(gh, pr, github_dir["sha"], f"{commit}:.github"), "workflows", commit)
    if workflows is None:
        return {}
    if workflows["type"] != "tree":
        refuse("workflow_paths_unreadable", f"{WORKFLOW_DIRECTORY} at {commit} is not a directory")
    files = {}
    for entry in _tree_entries(gh, pr, workflows["sha"], f"{commit}:{WORKFLOW_DIRECTORY}"):
        if not entry["path"].endswith(WORKFLOW_SUFFIXES):
            continue
        path = f"{WORKFLOW_DIRECTORY}/{entry['path']}"
        if entry["type"] != "blob" or entry["mode"] not in REGULAR_MODES:
            refuse("workflow_paths_unreadable", f"{path} at {commit} is not a regular file")
        files[path] = entry["sha"]
    return files


def blob_text(gh, pr, blob_sha: str, path: str) -> str:
    doc = api_json(gh, f"repos/{pr.owner}/{pr.repo}/git/blobs/{blob_sha}", "workflow_files_unreadable")
    if not isinstance(doc, dict) or doc.get("sha") != blob_sha or doc.get("encoding") != "base64":
        refuse("workflow_files_unreadable", f"{path}: blob {blob_sha} is malformed")
    content = doc.get("content")
    if not isinstance(content, str):
        refuse("workflow_files_unreadable", f"{path}: blob {blob_sha} has no content")
    try:
        return base64.b64decode("".join(content.split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        refuse("workflow_paths_unreadable", f"{path} is not base64 UTF-8 text")


def default_tip(gh, pr, default: str) -> str:
    """The default branch's tip commit, read from its ref."""
    ref = _quoted_branch(default, "main_tip_unreadable")
    doc = api_json(gh, f"repos/{pr.owner}/{pr.repo}/git/ref/heads/{ref}", "main_tip_unreadable")
    target = doc.get("object") if isinstance(doc, dict) else None
    if (
        not isinstance(doc, dict)
        or doc.get("ref") != f"refs/heads/{default}"
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or not isinstance(target.get("sha"), str)
        or OID.fullmatch(target["sha"]) is None
    ):
        refuse("main_tip_unreadable", f"refs/heads/{default} does not read as a commit")
    return target["sha"]


def merged_state(gh, pr) -> dict:
    """The pull request's state, merged flag, head and merge commit, for stage 11's confirmation."""
    doc = graphql(gh, MERGED_QUERY, pr.owner, pr.repo, pr.number, "merge_unconfirmed")
    try:
        pull = doc["data"]["repository"]["pullRequest"]
        commit = pull["mergeCommit"]
        return {
            "state": pull["state"],
            "merged": pull["merged"],
            "headRefOid": pull["headRefOid"],
            "mergeCommit": None if commit is None else commit["oid"],
        }
    except (KeyError, TypeError):
        refuse("merge_unconfirmed", "the merged-state read is malformed")
