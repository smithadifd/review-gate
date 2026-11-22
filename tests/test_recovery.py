"""Recover shares merge's closed grammar, except remediation is forbidden."""
import io

import pytest

from review_gate import recovery
from review_gate.records import RECORDS_METHODS, UnfilledRecords
from review_gate.grammar import RECOVERY_USAGE, USAGE, parse_merge_args, parse_recovery_args
from review_gate.refusal import Refusal
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


USAGE_CASES = [
    ([], "argument_missing"),
    (["recover", Repo().url, "--items", ITEM], "argument_missing"),
    (["merge", Repo().url, Repo().head, "--items", ITEM], "command_unknown"),
    (["recover", Repo().url, Repo().head, "--items", ITEM, "--receipts", "/x"], "argument_unknown"),
    (["recover", Repo().url, Repo().head, "--items", ITEM, "extra"], "argument_extra"),
]


@pytest.mark.parametrize("argv,reason", USAGE_CASES, ids=[f"{i}-{r}" for i, (_a, r) in enumerate(USAGE_CASES)])
def test_recovery_refusals_print_the_recover_usage(argv, reason):
    with pytest.raises(Refusal) as exc:
        parse_recovery_args(argv)
    assert exc.value.name == reason, exc.value.line()
    assert exc.value.detail.endswith("usage: " + RECOVERY_USAGE), exc.value.line()
    assert "review-gate merge" not in exc.value.detail, exc.value.line()


def test_merge_refusals_keep_the_merge_usage():
    with pytest.raises(Refusal) as exc:
        parse_merge_args([])
    assert exc.value.detail == "usage: " + USAGE, exc.value.line()


def test_recovery_accepts_the_exact_merge_subject():
    r = Repo()
    args = parse_recovery_args(["recover", r.url, r.head, "--items=" + ITEM])
    assert (args.url, args.sha, args.items, args.remediation) == (r.url, r.head, (ITEM,), None)
