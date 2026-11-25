"""Stage 0: the merge command's arguments, checked before any read.

    review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]

Nothing here reads GitHub, the records port or the filesystem. Every
argument is checked by full match, never trimmed or folded, and anything
outside the grammar refuses by name:

- the URL is ``https://github.com/<owner>/<repo>/pull/<N>``, the ledger's
  pull request URL grammar (owner and repo of letters, digits, ``_``, ``.``
  and ``-``; N a decimal number with no leading zero), with nothing before
  or after it; an owner or repo of ``.`` or ``..`` is refused too
  (``pr_url_invalid``);
- the sha is exactly 40 lowercase hex characters (``sha_invalid``); an
  uppercase sha is refused, never folded;
- ``--items`` is required, given once, as the next argument or as
  ``--items=<value>``: one or more item ids, comma separated, none empty and
  none repeated, each a letter or digit followed by up to 127 letters,
  digits, ``.``, ``_``, ``:`` or ``-``;
- ``--remediation`` is optional, given once, only as ``--remediation=<jobs>``:
  one or more job names, comma separated, none empty and none repeated, none
  with a control character or with leading or trailing whitespace. It is
  compared as an exact set with main's red jobs in stage 9;
- any other flag, a missing positional or an extra one refuses.

``review-gate recover`` takes the same grammar without ``--remediation``, and
its refusals print its own usage (``RECOVERY_USAGE``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .refusal import refuse

# The ledger's pull request URL grammar (ledger.receipts._PR_URL), matched whole.
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
DOT_SEGMENTS = (".", "..")
COMMAND = "merge"
USAGE = "review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]"
CHECK_COMMAND = "check"
CHECK_USAGE = "review-gate check <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]"
RECOVERY_COMMAND = "recover"
RECOVERY_USAGE = "review-gate recover <PR URL> <full head sha> --items ID[,ID...]"


@dataclass(frozen=True)
class PullRequest:
    """One pull request, named by its URL."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        """``owner/repo`` as the URL wrote it."""
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        """``owner/repo`` casefolded, the form record ids carry."""
        return self.slug.casefold()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


@dataclass(frozen=True)
class MergeArgs:
    pr: PullRequest
    url: str
    sha: str
    items: tuple
    remediation: frozenset | None


def parse_pr_url(url) -> PullRequest:
    if not isinstance(url, str):
        refuse("pr_url_invalid", repr(url))
    match = PR_URL.fullmatch(url)
    if match is None or match.group(1) in DOT_SEGMENTS or match.group(2) in DOT_SEGMENTS:
        refuse("pr_url_invalid", f"{url!r} is not https://github.com/OWNER/REPO/pull/N")
    return PullRequest(match.group(1), match.group(2), int(match.group(3)))


def parse_sha(sha) -> str:
    if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
        refuse("sha_invalid", "pass the full 40-character lowercase hex commit")
    return sha


def parse_items(value: str) -> tuple:
    if value == "":
        refuse("items_value_missing", "--items names no item")
    items = []
    for element in value.split(","):
        if element == "":
            refuse("items_element_empty", f"--items {value!r} has an empty element")
        if ITEM_ID.fullmatch(element) is None:
            refuse("items_id_invalid", f"{element!r} is not an item id")
        if element in items:
            refuse("items_id_duplicate", f"{element!r} is named twice")
        items.append(element)
    return tuple(items)


def _job_name_problem(name: str) -> str | None:
    if name != name.strip():
        return "has leading or trailing whitespace"
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        return "has a control character"
    return None


def parse_remediation(value: str) -> frozenset:
    if value == "":
        refuse("remediation_value_missing", "--remediation= names no job")
    jobs = []
    for element in value.split(","):
        if element == "":
            refuse("remediation_element_empty", f"--remediation={value!r} has an empty element")
        problem = _job_name_problem(element)
        if problem is not None:
            refuse("remediation_job_invalid", f"job name {element!r} {problem}")
        if element in jobs:
            refuse("remediation_job_duplicate", f"job {element!r} is named twice")
        jobs.append(element)
    return frozenset(jobs)


def parse_merge_args(argv) -> MergeArgs:
    return _parse_args(argv, command=COMMAND, usage=USAGE, remediation_allowed=True)


def parse_check_args(argv) -> MergeArgs:
    return _parse_args(argv, command=CHECK_COMMAND, usage=CHECK_USAGE, remediation_allowed=True)


def parse_recovery_args(argv) -> MergeArgs:
    """The merge grammar with verb ``recover`` and no remediation argument."""
    return _parse_args(argv, command=RECOVERY_COMMAND, usage=RECOVERY_USAGE, remediation_allowed=False)


def _parse_args(argv, *, command, usage, remediation_allowed) -> MergeArgs:
    """The checked arguments of ``command``; ``argv`` excludes the program name; refusals print ``usage``."""
    argv = list(argv)
    if not argv:
        refuse("argument_missing", f"usage: {usage}")
    if argv[0] != command:
        refuse("command_unknown", f"{argv[0]!r}; usage: {usage}")
    positionals = []
    items_value = None
    remediation_value = None
    index = 1
    while index < len(argv):
        token = argv[index]
        if not isinstance(token, str):
            refuse("argument_unknown", repr(token))
        if token == "--items" or token.startswith("--items="):
            if items_value is not None:
                refuse("items_given_twice", "--items is given more than once")
            if token == "--items":
                if index + 1 >= len(argv):
                    refuse("items_value_missing", "--items has no value")
                index += 1
                items_value = argv[index]
            else:
                items_value = token[len("--items="):]
        elif token == "--remediation" or token.startswith("--remediation="):
            if not remediation_allowed:
                refuse("argument_unknown", "--remediation is not part of recover")
            if remediation_value is not None:
                refuse("remediation_given_twice", "--remediation is given more than once")
            if token == "--remediation":
                refuse(
                    "remediation_value_missing",
                    "--remediation must name main's red jobs: --remediation=JOB[,JOB...]",
                )
            remediation_value = token[len("--remediation="):]
        elif token.startswith("-"):
            refuse("argument_unknown", f"{token!r}; usage: {usage}")
        else:
            if len(positionals) == 2:
                refuse("argument_extra", f"{token!r}; usage: {usage}")
            positionals.append(token)
        index += 1
    if len(positionals) < 2:
        refuse("argument_missing", f"a PR URL and a full head sha are required; usage: {usage}")
    url, sha = positionals
    pr = parse_pr_url(url)
    sha = parse_sha(sha)
    if items_value is None:
        refuse("items_missing", f"--items is required: name the items this merge closes; usage: {usage}")
    items = parse_items(items_value)
    remediation = None if remediation_value is None else parse_remediation(remediation_value)
    return MergeArgs(pr=pr, url=pr.url, sha=sha, items=items, remediation=remediation)


@dataclass(frozen=True)
class ImportArgs:
    store: str
    pr: PullRequest


def parse_import_args(argv):
    """Exactly import --store ABSOLUTE_DIRECTORY --pr PR_URL, in either flag order."""
    argv = list(argv)
    if not argv or argv[0] != "import":
        refuse("command_unknown", "expected import")
    if len(argv) != 5 or {argv[1], argv[3]} != {"--store", "--pr"}:
        refuse("argument_unknown", "usage: review-gate import --store <dir> --pr <URL>")
    values = {argv[1]: argv[2], argv[3]: argv[4]}
    directory = values["--store"]
    if not isinstance(directory, str) or not directory.startswith("/") or "\0" in directory:
        refuse("import_store_relative", "--store must be an absolute folder")
    return ImportArgs(directory, parse_pr_url(values["--pr"]))


def parse_record_args(argv):
    """Exactly record: no flag, no operand. The document is standard input."""
    argv = list(argv)
    if not argv or argv[0] != "record":
        refuse("command_unknown", "expected record")
    if len(argv) != 1:
        refuse("argument_unknown", "usage: review-gate record (one document on standard input)")


@dataclass(frozen=True)
class ItemArgs:
    pr: PullRequest
    lookup: bool


def parse_item_args(argv):
    """Exactly item --pr URL [--lookup], with --lookup in either flag position.

    Flags are case-sensitive, given once, without equals forms. Unknown,
    repeated, missing or extra tokens refuse argument_unknown; a malformed
    URL refuses pr_url_invalid. No reads precede this grammar.
    """
    argv = list(argv)
    if not argv or argv[0] != "item":
        refuse("command_unknown", "expected item")
    lookup = "--lookup" in argv[1:]
    flags = argv[1:]
    if lookup:
        flags = list(flags)
        flags.remove("--lookup")
    if len(flags) != 2 or flags[0] != "--pr":
        refuse("argument_unknown", "usage: review-gate item --pr <URL> [--lookup]")
    return ItemArgs(parse_pr_url(flags[1]), lookup)
