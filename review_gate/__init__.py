"""review-gate: a head-pinned merge command that reads review records and live GitHub state.

The merge command runs the stages of ``review_gate.gate.STAGE_ORDER`` in that
order and refuses by name at the first one that does not pass. This release
carries the stages that need no ledger (store, state reads, CI, main green,
recheck and the write-ahead shape) and leaves stages 3 to 7 as calls on the
records port. The shipped records port is unfilled: every call on it refuses
``records_unavailable``, so this release never merges anything.
"""

__version__ = "0.3.1"
