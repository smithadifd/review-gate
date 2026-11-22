"""Record one writer document read from standard input."""
from __future__ import annotations

import hashlib
import sys

from .grammar import parse_record_args
from .receipt_document import CAP
from .receipt_writer import write_receipt
from .refusal import Refusal, refuse


def _read(stdin):
    """At most CAP + 1 bytes; anything past the cap is refused, never cut to fit."""
    data = stdin.read(CAP + 1)
    if not data:
        refuse("record_empty", "standard input held no document")
    if len(data) > CAP:
        refuse("receipt_doc_too_large", f"the inline limit is {CAP} bytes")
    return data


def _known(records, store_check, digest):
    """Ids of the receipts already recorded for these exact bytes, read under admission."""
    store_check.check(records.database_paths())
    with records._open() as lg:
        return {e.id for e in lg.events(kind="review.receipt")
                if e.payload.get("doc_sha256") == digest}


def run(argv, *, records, gh, store_check, out, err, stdin=None):
    try:
        parse_record_args(argv)
        document = _read(sys.stdin.buffer if stdin is None else stdin)
        before = _known(records, store_check, hashlib.sha256(document).hexdigest())
        event = write_receipt(document, records=records, store_check=store_check)
        p = event.payload
        state = "already_recorded" if event.id in before else "written"
        out.write(f"{state} https://github.com/{p['repo']}/pull/{p['pr']} "
                  f"round={p['round']} lane={p['lane']}\n")
        return 0
    except Refusal as exc:
        err.write(exc.line() + "\n")
        return 2
    except Exception as exc:
        err.write(f"record_failed: {type(exc).__name__}\n")
        return 2
