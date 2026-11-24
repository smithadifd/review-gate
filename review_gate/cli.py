"""The ``review-gate`` command line.

    review-gate merge <PR URL> <full head sha> --items ID[,ID...] [--remediation=JOB[,JOB...]]
    review-gate recover <PR URL> <full head sha> --items ID[,ID...]
    review-gate import --store <absolute directory> --pr <PR URL>
    review-gate record < one-document.json
    review-gate item --pr <PR URL> [--lookup]

LEDGER_DB names an absolute database file. Unset or empty refuses
``records_unavailable``; a nonempty value selects the ledger-backed port.
The recovery verb records an already completed merge and never merges. The record verb
takes no argument: it reads one receipt document (at most 65536 bytes) from standard input.
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
from .receipt_import import run as import_receipts
from .receipt_record import run as record_receipt
from .pr_item import run as resolve_item
from .store import StoreCheck


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    runner = (resolve_item if argv and argv[0] == "item" else
              import_receipts if argv and argv[0] == "import" else
              record_receipt if argv and argv[0] == "record" else
              recover if argv and argv[0] == "recover" else run)
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
