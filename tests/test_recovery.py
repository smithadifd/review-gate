"""Recover shares merge's closed grammar, except remediation is forbidden."""
import io

import pytest

from review_gate import recovery
from review_gate.records import RECORDS_METHODS, UnfilledRecords
from review_gate.grammar import parse_recovery_args
from rig import FakeGh, Repo, ITEM
from test_arguments import CASES, CountingStore


RECOVERY_CASES = []
for argv, reason in CASES:
    transformed = ["recover", *argv[1:]] if argv and argv[0] == "merge" else argv
    if any(isinstance(a, str) and a.startswith("--remediation") for a in argv):
        reason = "argument_unknown"
    RECOVERY_CASES.append((transformed, reason))


@pytest.mark.parametrize("argv,reason", RECOVERY_CASES)
def test_recovery_grammar_refuses_before_any_read(tmp_path, argv, reason):
    calls = []

    class Port(UnfilledRecords):
        def __getattribute__(self, name):
            if name in RECORDS_METHODS:
                calls.append(name)
            return object.__getattribute__(self, name)

    fake = FakeGh(tmp_path / "gh")
    store = CountingStore()
    out, err = io.StringIO(), io.StringIO()
    code = recovery.run(argv, records=Port(), gh=fake.transport(), store_check=store, out=out, err=err)
    assert code == 2
    assert err.getvalue().split(":", 1)[0] == reason, err.getvalue()
    assert out.getvalue() == ""
    assert calls == [] and fake.calls() == [] and store.calls == 0


def test_recovery_accepts_the_exact_merge_subject():
    r = Repo()
    args = parse_recovery_args(["recover", r.url, r.head, "--items=" + ITEM])
    assert (args.url, args.sha, args.items, args.remediation) == (r.url, r.head, (ITEM,), None)
