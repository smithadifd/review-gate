"""The ``review-gate`` command line.

    review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]

The command is wired to the shipped records port, which is unfilled in this
release, so it refuses every pull request (``records_unavailable``) and never
merges. Nothing on the command line or in the environment selects another
port.
"""
from __future__ import annotations

import shutil
import sys

from .gate import run
from .gh import GhTransport
from .records import UnfilledRecords
from .store import StoreCheck


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    return run(
        argv,
        records=UnfilledRecords(),
        gh=GhTransport(shutil.which("gh")),
        store_check=StoreCheck.shipped(),
        out=sys.stdout,
        err=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
