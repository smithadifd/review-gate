"""The ``gh`` process boundary: every GitHub read and the merge call go through here.

``GhTransport`` runs the ``gh`` executable it was built with and returns its
exit code, stdout and stderr. It decides nothing. The shipped command builds
it from ``gh`` on PATH; a test builds it with the path of a fake ``gh`` from
Python. With no executable, every call refuses ``gh_unavailable``.
"""
from __future__ import annotations

import json
import subprocess
from typing import NamedTuple

from .refusal import refuse


class GhResult(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


class GhTransport:
    def __init__(self, executable: str | None) -> None:
        self.executable = executable

    def run(self, args: list) -> GhResult:
        if not self.executable:
            refuse("gh_unavailable", "the gh command is not on PATH")
        try:
            done = subprocess.run(
                [self.executable, *args], capture_output=True, stdin=subprocess.DEVNULL, check=False
            )
        except OSError as exc:
            refuse("gh_unavailable", f"gh could not start: {exc.strerror or exc}")
        return GhResult(done.returncode, done.stdout, done.stderr)


def _tail(stderr: bytes) -> str:
    said = stderr.decode("utf-8", "replace").strip().splitlines()
    return f": {said[-1][:200]}" if said else ""


def call(gh, args: list, name: str, what: str) -> bytes:
    """gh's stdout for ``args``, or a refusal named ``name`` when gh exits nonzero."""
    result = gh.run(list(args))
    if result.returncode != 0:
        refuse(name, f"{what}: gh exited {result.returncode}{_tail(result.stderr)}")
    return result.stdout


def call_json(gh, args: list, name: str, what: str):
    raw = call(gh, args, name, what)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        refuse(name, f"{what}: the response is not JSON")


def api_json(gh, endpoint: str, name: str):
    """One REST read, ``gh api <endpoint>``, parsed."""
    return call_json(gh, ["api", endpoint], name, f"gh api {endpoint}")


def api_pages(gh, endpoint: str, name: str) -> list:
    """Every page of a REST read, ``gh api --paginate --slurp <endpoint>``; a non-list refuses."""
    pages = call_json(gh, ["api", "--paginate", "--slurp", endpoint], name, f"gh api {endpoint}")
    if not isinstance(pages, list):
        refuse(name, f"gh api {endpoint} returned no page list")
    return pages


def graphql(gh, query: str, owner: str, name: str, number: int, refusal: str):
    """One GraphQL read with the owner, name and number variables, parsed."""
    return call_json(
        gh,
        ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}",
         "-F", f"number={number}"],
        refusal,
        "gh api graphql",
    )
