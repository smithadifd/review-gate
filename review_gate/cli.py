"""The ``review-gate`` command line.

    review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]

LEDGER_DB names an absolute database file. Unset or empty refuses
``records_unavailable``; a nonempty value selects the ledger-backed port.
The recovery verb records an already completed merge and never merges.
"""
from __future__ import annotations

import os
import shutil
import sys

from .gate import run
from .gh import GhTransport
from .records import UnfilledRecords
from .ledger_records import LedgerRecords
from .recovery import run as recover
from .store import StoreCheck


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    runner = recover if argv and argv[0] == "recover" else run
    database = os.environ.get("LEDGER_DB")
    return runner(
        argv,
        records=LedgerRecords(database) if database else UnfilledRecords(),
        gh=GhTransport(shutil.which("gh")),
        store_check=StoreCheck.shipped(),
        out=sys.stdout,
        err=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
