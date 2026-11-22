#!/usr/bin/env python3
"""Fail when the tree or the history names a person, a machine, a home path or a private host.

Usage: check-no-person.py

The repository checked is the one that holds this script (the folder above
``scripts/``), whatever the working directory; it must be the top of a git
work tree. There are no options. The history becomes public with the first
tag, so the scan covers it as well as the tree:

- the tree: every tracked file, read from the git index (its staged blob,
  which is the checked-out commit in CI), binary files included, and every
  tracked path name;
- the history: every commit reachable from HEAD, read from its raw object:
  its message, author and committer identities (name and email), and every
  path and blob in its tree. Blob bytes are read once per object id; rules
  and allowances are applied separately for each path that names the blob.

Rules, over every line:

- Shape rules, held in this file: home folder paths, encoded home paths,
  ``~/`` paths into private folders, a secrets folder under the home
  folder, private-network host names, decision numbers and private schema
  names, plus token shapes and private-key headers.
- The deny list: person, owner and private-system words come only from the
  environment variable NO_PERSON_DENY, a case-insensitive regular
  expression of terms joined by ``|`` (CI fills it from the repository
  secret of that name). No word is spelled in a tracked file. A match is
  allowed only when the pair (the file's path, the sha256 of the lowercased
  matched text) is a row of ``scripts/no-person-allowance.tsv``. For a
  commit's author or committer identity the path is the pseudo path
  ``:commit-identity``. A commit message has no allowance.
- The license carve: the MIT copyright line of the root ``LICENSE`` file,
  exactly ``Copyright (c) <year> <holder>`` with the holder pinned by its
  sha256 (``LICENSE_HOLDER_SHA256``), is exempt from the deny list, once.

Output, one line per hit, never the matched text and never the variable:
``<path>:<line>: <rule>``, ``<path>: path name: <rule>``, ``tracked path
#<n>: deny-list (path name)``, ``commit <sha12>: message line <n>: <rule>``,
``commit <sha12>: author: <rule>``, ``commit <sha12>: committer: <rule>`` or
``commit <sha12>: <path>:<line>: <rule>`` for historical file contents,
``scripts/no-person-allowance.tsv:<line>: stale allowance`` for a row that
matched nothing, ``<path>: unreadable (<reason>)`` and ``no-person:
<reason>`` for a configuration failure.

NO_PERSON_DENY unset or empty: in CI (``CI=true`` or ``GITHUB_ACTIONS=true``)
that is a failure, since the pattern list is missing; elsewhere the shape
rules still run and one WARN line says the deny list did not run. An
expression that does not compile, or that matches the empty string, is a
failure everywhere.

Exit 0 with one ``no-person: ok`` line when no rule fires; exit 1 on any
hit, a stale allowance row, an unreadable file, a configuration failure or a
git failure. Nothing is skipped silently.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "no-person:"
DENY_VAR = "NO_PERSON_DENY"
ALLOWANCE = "scripts/no-person-allowance.tsv"
IDENTITY_PATH = ":commit-identity"
LICENSE_PATH = "LICENSE"
LICENSE_LINE = re.compile(r"Copyright \(c\) [0-9]{4} (\S(?:.*\S)?)")
LICENSE_HOLDER_SHA256 = "c157acfd0136907586269d8508b72f480981d020bf98eda12b47a8a0a82f0093"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OID = re.compile(r"[0-9a-f]{40}")
_IDENTITY = re.compile(r"(.*) <(.*)> [0-9]+ [+-][0-9]{4}")
_GIT_LOCATION_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
})

# Shapes are built from fragments so this file does not carry what it forbids.
HOME_HOST = re.compile(
    r"(?<!(?-i:[A-Za-z0-9_.-]))(?!(?-i:self\.))(?:[A-Za-z0-9-]+\.)+ho" + r"me"
    + r"(?!(?-i:[A-Za-z0-9(]))",
    re.IGNORECASE,
)
WORD_START = r"(?<!(?-i:[A-Za-z0-9_]))"
WORD_END = r"(?!(?-i:[A-Za-z0-9_]))"
SHAPES = [
    ("absolute home", re.compile(r"/Us" + r"ers/[^/\s`'\")]+", re.IGNORECASE)),
    ("linux home", re.compile(r"/ho" + r"me/[^/\s`'\")]+", re.IGNORECASE)),
    ("encoded home", re.compile(r"-Us" + r"ers-[a-z]", re.IGNORECASE)),
    ("private-network hostname", HOME_HOST),
    ("private home folder",
     re.compile(r"~/(?:\.[\w-]+|co" + r"de|wo" + r"rk|Library|Documents|Desktop)" + WORD_END,
                re.IGNORECASE)),
    ("private config path", re.compile(r"~/(?:[^/\s]+/)*sec" + r"rets" + WORD_END, re.IGNORECASE)),
    ("decision number", re.compile(WORD_START + r"AD" + r"R[ -]?\d{3,4}" + WORD_END, re.IGNORECASE)),
    ("private schema", re.compile(WORD_START + r"act\." + r"v\d" + WORD_END, re.IGNORECASE)),
]
SECRET_SHAPES = [
    ("token", re.compile(r"gh" + r"p_[A-Za-z0-9]{20}")),
    ("token", re.compile(r"s" + r"k-[A-Za-z0-9]{20}")),
    ("token", re.compile(r"xo" + r"x[bp]-")),
    ("token", re.compile(r"AK" + r"IA[0-9A-Z]{16}")),
    ("private key", re.compile(r"BEGIN [A-Z ]*PRI" + r"VATE KEY")),
]


class CheckError(RuntimeError):
    """A git failure or an unreadable input (exit 1, as every failure)."""


def _git_env() -> dict:
    env = {
        key: value for key, value in os.environ.items()
        if key not in _GIT_LOCATION_ENV
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def git(args: list, root: Path, data: bytes | None = None) -> bytes:
    try:
        done = subprocess.run(["git", *args], cwd=root, env=_git_env(), input=data,
                              capture_output=True, check=False)
    except OSError as exc:
        raise CheckError(f"git is not available ({exc.strerror or exc})") from exc
    if done.returncode != 0:
        message = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CheckError(f"git {args[0]} failed: {message[-1] if message else done.returncode}")
    return done.stdout


def require_top(root: Path) -> None:
    top = git(["rev-parse", "--show-toplevel"], root).decode("utf-8", "replace").strip()
    if Path(top).resolve() != root.resolve():
        raise CheckError(f"{root} is not the top of a git work tree")


def tracked(root: Path) -> list:
    """(path, mode, blob id) for every index entry, in ``git ls-files`` order."""
    out = git(["ls-files", "--stage", "-z"], root)
    entries = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, blob, _stage = meta.decode("ascii").split(" ")
        entries.append((name.decode("utf-8", "surrogateescape"), mode, blob))
    return entries


def read_objects(root: Path, ids: list, kind: str) -> dict:
    """The bytes of each object id of ``kind``, through one ``git cat-file --batch``."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return {}
    out = git(["cat-file", "--batch"], root, ("\n".join(unique) + "\n").encode("ascii"))
    found = {}
    position = 0
    for object_id in unique:
        end = out.index(b"\n", position)
        header = out[position:end].decode("ascii", "replace").split(" ")
        position = end + 1
        if len(header) != 3 or header[0] != object_id or header[1] != kind:
            raise CheckError(f"git cat-file did not return {kind} {object_id}")
        size = int(header[2])
        found[object_id] = out[position:position + size]
        position += size + 1
    return found


def history(root: Path) -> list:
    """(sha, author, committer, message) of every commit reachable from HEAD, newest first."""
    ids = git(["rev-list", "HEAD"], root).decode("ascii").split()
    if not ids or any(_OID.fullmatch(each) is None for each in ids):
        raise CheckError("git rev-list HEAD named no commit")
    raw = read_objects(root, ids, "commit")
    commits = []
    for sha in ids:
        text = raw[sha].decode("utf-8", "surrogateescape")
        header, _, message = text.partition("\n\n")
        identities = {}
        for line in header.split("\n"):
            field, _, value = line.partition(" ")
            if field in ("author", "committer"):
                match = _IDENTITY.fullmatch(value)
                if match is None:
                    raise CheckError(f"commit {sha[:12]} has an unreadable {field} line")
                identities[field] = f"{match.group(1)} <{match.group(2)}>"
        if set(identities) != {"author", "committer"}:
            raise CheckError(f"commit {sha[:12]} lacks an author or a committer")
        commits.append((sha, identities["author"], identities["committer"], message))
    return commits


def compile_deny(value: str):
    try:
        pattern = re.compile(value, re.IGNORECASE)
    except (re.error, RecursionError, OverflowError, ValueError):
        raise CheckError(f"{DENY_VAR} is not a valid regular expression") from None
    if pattern.search("") is not None:
        raise CheckError(f"{DENY_VAR} matches the empty string")
    return pattern


def digest(matched: str) -> str:
    return hashlib.sha256(matched.lower().encode("utf-8", "surrogateescape")).hexdigest()


def _path_problem(path: str) -> str | None:
    if path.startswith("/"):
        return "has a leading slash"
    for part in path.split("/"):
        if part == "":
            return "has an empty component"
        if part in {".", ".."}:
            return f"has a {part!r} component"
    return None


def parse_allowance(text: str) -> tuple:
    """((path, hash) -> line number, problems) from the allowance file."""
    if "\r" in text:
        line = text[: text.index("\r")].count("\n") + 1
        return {}, [f"{ALLOWANCE}:{line}: carriage return (CRLF line endings are refused)"]
    if not text.endswith("\n"):
        return {}, [f"{ALLOWANCE}: does not end with a newline"]
    pairs = {}
    problems = []
    for number, line in enumerate(text[:-1].split("\n"), 1):
        if line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            problems.append(f"{ALLOWANCE}:{number}: not a row of <path> TAB <sha256> TAB <reason>")
            continue
        path, value, reason = fields
        problem = _path_problem(path)
        if problem is not None:
            problems.append(f"{ALLOWANCE}:{number}: the path {problem}")
        elif not _HEX64.fullmatch(value):
            problems.append(f"{ALLOWANCE}:{number}: the hash is not 64 lowercase hex")
        elif not reason.strip():
            problems.append(f"{ALLOWANCE}:{number}: the reason is empty")
        elif (path, value) in pairs:
            problems.append(f"{ALLOWANCE}:{number}: duplicate pair of line {pairs[(path, value)]}")
        else:
            pairs[(path, value)] = number
    return pairs, problems


def license_copyright_line(path: str, line: str) -> bool:
    if path != LICENSE_PATH:
        return False
    match = LICENSE_LINE.fullmatch(line)
    if match is None:
        return False
    holder = match.group(1).encode("utf-8", "surrogateescape")
    return hashlib.sha256(holder).hexdigest() == LICENSE_HOLDER_SHA256


def line_rules(line: str, deny, allow_path: str | None, allowed: dict, used: set, carve: bool = False) -> list:
    """The rules ``line`` breaks; a deny match is allowed only by an (allow_path, hash) row."""
    rules = []
    for name, pattern in SHAPES:
        if pattern.search(line):
            rules.append(f"no-person shape: {name}")
    for name, pattern in SECRET_SHAPES:
        if pattern.search(line):
            rules.append(f"secret shape: {name}")
    if deny is not None and not carve:
        for match in deny.finditer(line):
            if not match.group(0):
                continue
            pair = (allow_path, digest(match.group(0)))
            if allow_path is not None and pair in allowed:
                used.add(pair)
            else:
                rules.append("deny-list")
    return list(dict.fromkeys(rules))


def scan_text(path: str, data: bytes, deny, allowed: dict, used: set) -> list:
    text = data.decode("utf-8", errors="surrogateescape")
    hits = []
    carved = False
    for number, line in enumerate(text.split("\n"), 1):
        carve = not carved and license_copyright_line(path, line)
        carved = carved or carve
        for rule in line_rules(line, deny, path, allowed, used, carve):
            hits.append(f"{path}:{number}: {rule}")
    return hits


def scan_history(commits: list, deny, allowed: dict, used: set) -> list:
    hits = []
    for sha, author, committer, message in commits:
        short = sha[:12]
        for role, identity in (("author", author), ("committer", committer)):
            for rule in line_rules(identity, deny, IDENTITY_PATH, allowed, used):
                hits.append(f"commit {short}: {role}: {rule}")
        for number, line in enumerate(message.split("\n"), 1):
            for rule in line_rules(line, deny, None, allowed, used):
                hits.append(f"commit {short}: message line {number}: {rule}")
    return hits


def scan_historical_blobs(root: Path, commits: list, blobs: dict, deny, allowed: dict, used: set) -> list:
    """Read each historical blob once, retaining its paths for path-scoped allowances."""
    locations = {}
    for sha, _author, _committer, _message in commits:
        tree = git(["ls-tree", "-r", "-z", "--full-tree", sha], root)
        for record in tree.split(b"\0"):
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            _mode, kind, oid = meta.decode("ascii").split(" ")
            path = name.decode("utf-8", "surrogateescape")
            locations.setdefault((kind, oid), {}).setdefault(path, sha)
    blobs.update(read_objects(
        root, [oid for kind, oid in locations if kind == "blob" and oid not in blobs], "blob",
    ))
    hits = []
    for (kind, oid), paths in locations.items():
        for number, (path, sha) in enumerate(paths.items(), 1):
            prefix = f"commit {sha[:12]}: "
            denied_path = deny is not None and deny.search(path)
            label = f"historical path #{number}" if denied_path else path
            for name, pattern in SHAPES:
                if pattern.search(path):
                    hits.append(f"{prefix}{label}: path name: no-person shape: {name}")
            if denied_path:
                hits.append(f"{prefix}{label}: deny-list (path name)")
            if kind != "blob":
                hits.append(f"{prefix}{label}: unreadable (a submodule entry, not a file)")
                continue
            for finding in scan_text(path, blobs[oid], deny, allowed, used):
                hits.append(prefix + label + finding[len(path):])
    return hits


def check(root: Path, environ: dict) -> tuple:
    """(findings, warnings, files scanned, commits scanned)."""
    findings = []
    warnings = []
    in_ci = environ.get("CI") == "true" or environ.get("GITHUB_ACTIONS") == "true"
    value = environ.get(DENY_VAR, "")
    deny = None
    if not value:
        if in_ci:
            findings.append(f"{PREFIX} {DENY_VAR} is unset or empty in CI; the deny list "
                            "cannot be skipped there")
        else:
            warnings.append(f"{PREFIX} WARN: {DENY_VAR} IS UNSET: the deny list did not run; "
                            "only the shape rules were checked")
    else:
        try:
            deny = compile_deny(value)
        except CheckError as exc:
            findings.append(f"{PREFIX} {exc}")
    require_top(root)
    entries = tracked(root)
    by_path = {path: (mode, blob) for path, mode, blob in entries}
    allowed = {}
    allowance = by_path.get(ALLOWANCE)
    if allowance is None or allowance[0] not in {"100644", "100755"}:
        findings.append(f"{ALLOWANCE}: unreadable (not tracked as a regular file)")
    blobs = read_objects(root, [blob for _path, mode, blob in entries if mode != "160000"], "blob")
    if allowance is not None and allowance[0] in {"100644", "100755"}:
        try:
            allowed, problems = parse_allowance(blobs[allowance[1]].decode("utf-8"))
        except UnicodeDecodeError:
            allowed, problems = {}, [f"{ALLOWANCE}: unreadable (not UTF-8 text)"]
        findings.extend(problems)
    used = set()
    for number, (path, mode, blob) in enumerate(entries, 1):
        for name, pattern in SHAPES:
            if pattern.search(path):
                findings.append(f"{path}: path name: no-person shape: {name}")
        if deny is not None and deny.search(path):
            findings.append(f"tracked path #{number}: deny-list (path name)")
        if mode == "160000":
            findings.append(f"{path}: unreadable (a submodule entry, not a file)")
            continue
        findings.extend(scan_text(path, blobs[blob], deny, allowed, used))
    commits = history(root)
    findings.extend(scan_history(commits, deny, allowed, used))
    findings.extend(scan_historical_blobs(root, commits, blobs, deny, allowed, used))
    if deny is not None:
        for pair, line in sorted(allowed.items(), key=lambda item: item[1]):
            if pair not in used:
                findings.append(f"{ALLOWANCE}:{line}: stale allowance (the pair matched nothing in this scan)")
    return findings, warnings, len(entries), len(commits)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print(f"{PREFIX} ERROR: takes no arguments", file=sys.stderr)
        return 1
    try:
        findings, warnings, files, commits = check(ROOT, dict(os.environ))
    except CheckError as exc:
        print(f"{PREFIX} ERROR: {exc}")
        return 1
    for warning in warnings:
        print(warning)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{PREFIX} FAIL: {len(findings)} finding(s)")
        return 1
    state = "unset (WARN above)" if warnings else "set"
    print(f"{PREFIX} ok: {files} tracked files and {commits} commits scanned; deny list {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
