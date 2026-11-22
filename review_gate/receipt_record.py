"""Record one writer document read from standard input."""
from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

from .grammar import parse_record_args
from .receipt_document import CAP, derive_document
from .receipt_import import _recorded
from .receipt_writer import write_receipt
from .refusal import Refusal, refuse


def _read(stdin):
    """At most CAP + 1 bytes; anything past the cap is refused, never cut to fit."""
    data = stdin.read(CAP + 1) if stdin is not None else b""
    if not data:
        refuse("record_empty", "standard input held no document")
    if len(data) > CAP:
        refuse("receipt_doc_too_large", f"the inline limit is {CAP} bytes")
    return data


def _known(records, store_check, derived, digest):
    """The receipt already recorded for these exact bytes in their slot, read under admission.

    The rule is import's skip: a digest recorded for its slot (lane, round, head) is that
    document's receipt, whatever its provenance or recorded base.
    """
    fields = derived.fields
    pr = SimpleNamespace(key=fields["repo"], number=fields["pr"])
    slot = (fields["lane"], fields["round"], fields["head"], digest)
    store_check.check(records.database_paths())
    with records._open() as lg:
        return next((e for e in lg.events(kind="review.receipt") if slot in _recorded([e], pr)), None)


def run(argv, *, records, gh, store_check, out, err, stdin=None):
    try:
        parse_record_args(argv)
        document = _read(getattr(sys.stdin, "buffer", None) if stdin is None else stdin)
        derived = derive_document(document)
        known = _known(records, store_check, derived, hashlib.sha256(document).hexdigest())
        if known is None:
            written = write_receipt(document, records=records, store_check=store_check)
            p = written.event.payload
            state = "written" if written.appended else "already_recorded"
        else:
            p, state = known.payload, "already_recorded"
        out.write(f"{state} https://github.com/{p['repo']}/pull/{p['pr']} "
                  f"round={p['round']} lane={p['lane']}\n")
        return 0
    except Refusal as exc:
        err.write(exc.line() + "\n")
        return 2
    except Exception as exc:
        err.write(f"record_failed: {type(exc).__name__}\n")
        return 2
