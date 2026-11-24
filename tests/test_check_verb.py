"""Read-only preflight shares merge's decisions through the live recheck."""
from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from review_gate import cli, gate
from review_gate.grammar import parse_merge_args
from review_gate.refusal import Refusal
from rig import Result, Rig, oid


CHECK_USAGE = (
    "review-gate check <PR URL> <full head sha> --items ID[,ID...] "
    "[--remediation=JOB[,JOB...]]"
)


def invoke(rig, *, verb="check", argv=None, **keywords):
    runner = getattr(gate, verb if verb == "check" else "run", None)
    assert callable(runner), "gate.check must expose the read-only sequencer"
    rig.fake.set_routes(rig.repo.routes())
    out, err = io.StringIO(), io.StringIO()
    code = runner([verb, *rig.repo.argv()[1:]] if argv is None else argv,
                  records=rig.port, gh=rig.fake.transport(), store_check=rig.store.check,
                  out=out, err=err, **keywords)
    return Result(code, out.getvalue(), err.getvalue())


def forbid_writes(rig, monkeypatch):
    spies = {}
    for name in ("merge_state", "write_intent", "write_finished", "close_items"):
        spies[name] = Mock(side_effect=AssertionError(f"check called {name}"))
        monkeypatch.setattr(rig.port, name, spies[name])
    return spies


def test_pass_reads_through_recheck_without_merge_state_or_writes(tmp_path, monkeypatch):
    merged = Rig(tmp_path / "merge")
    assert merged.run().code == 0
    rig = Rig(tmp_path / "check")
    spies = forbid_writes(rig, monkeypatch)
    result = invoke(rig)
    assert result.code == 0, result.err
    assert result.out == f"would-merge {rig.repo.url} head={rig.repo.head}\n"
    assert result.err == ""
    assert rig.merge_calls() == []
    for spy in spies.values():
        spy.assert_not_called()
    assert rig.port.names() == [
        "database_paths", "item", "receipts", "open_bugs", "hardening", "round_cap",
        "item", "receipts", "open_bugs", "hardening", "round_cap",
    ]


def refuse_at(rig, stage, monkeypatch):
    if stage == "store":
        rig.store.check = Mock()
        rig.store.check.check.side_effect = Refusal("store_in_git", "fixture database is in a git tree")
    elif stage == "state_read":
        rig.repo.state1["headRefOid"] = oid("new head")
    elif stage in ("item", *gate.RECORDS_STAGES):
        monkeypatch.setattr(rig.port, stage,
                            Mock(side_effect=Refusal("records_answer_invalid", f"blocked {stage}")))
    elif stage == "ci":
        rig.repo.pr_runs = []
    elif stage == "main_green":
        rig.repo.push_runs["ci.yml"] = []
    elif stage == "recheck":
        # The second live read occurs only after stage 9 has judged main.
        rig.repo.state2["headRefOid"] = oid("moved after main green")
    else:
        raise AssertionError(stage)


@pytest.mark.parametrize("stage", [
    "store", "state_read", "item", "receipts", "open_bugs", "hardening", "round_cap",
    "ci", "main_green", "recheck",
])
def test_refusal_parity_through_recheck(tmp_path, monkeypatch, stage):
    merged, checked = Rig(tmp_path / "merge"), Rig(tmp_path / "check")
    for rig in (merged, checked):
        refuse_at(rig, stage, monkeypatch)
    expected, actual = invoke(merged, verb="merge"), invoke(checked)
    assert expected.code == 2, expected.err
    assert actual.code == 2, actual.out
    assert actual.err == expected.err
    assert actual.out == expected.out == ""
    assert checked.merge_calls() == []
    assert checked.port.names() == merged.port.names()
    assert checked.fake.calls() == merged.fake.calls()


@pytest.mark.parametrize("tail,reason", [
    ([], "items_missing"),
    (["--items", "it-example", "--unknown"], "argument_unknown"),
    (["--items", "it-example", "extra"], "argument_extra"),
    (["--items", "it-example", "--ITEMS=x"], "argument_unknown"),
    (["--items", "it-example", " --items=x"], "argument_extra"),
])
def test_argument_refusals_use_check_usage(rig, tail, reason):
    result = invoke(rig, argv=["check", rig.repo.url, rig.repo.head, *tail])
    assert result.code == 2
    assert result.reason == reason
    assert f"usage: {CHECK_USAGE}" in result.err
    assert result.out == ""
    assert rig.port.calls == rig.fake.calls() == []


@pytest.mark.parametrize("verb", ["merge", "Check", " check", "-check", "```check"])
def test_check_parser_rejects_other_commands(rig, verb):
    result = invoke(rig, argv=[verb, *rig.repo.argv()[1:]])
    assert result.code == 2
    assert result.err == f"command_unknown: {verb!r}; usage: {CHECK_USAGE}\n"
    assert rig.port.calls == rig.fake.calls() == []


def test_check_accepts_merge_remediation_grammar(rig):
    from review_gate.grammar import parse_check_args
    argv = rig.repo.argv("--remediation=test,lint")
    assert parse_check_args(["check", *argv[1:]]) == parse_merge_args(argv)


@pytest.mark.parametrize("verb,entry", [("check", "check"), ("merge", "run")])
def test_cli_dispatches_without_exposing_library_stage(rig, monkeypatch, verb, entry):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    spy = Mock(return_value=0)
    monkeypatch.setattr(cli, entry, spy, raising=False)
    argv = [verb, *rig.repo.argv()[1:]]
    assert cli.main(argv) == 0
    assert spy.call_args.args == (argv,)
    assert set(spy.call_args.kwargs) == {"records", "gh", "store_check", "out", "err"}


def test_cli_without_database_refuses_check(rig, monkeypatch, capsys):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    assert cli.main(["check", *rig.repo.argv()[1:]]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("records_unavailable:")


def test_ledger_backed_check_appends_no_events(ledger_rig, monkeypatch, capsys):
    from ledger import open_ledger
    rig = ledger_rig
    from functools import partial
    from review_gate.ledger_records import LedgerRecords
    rig.documented_receipt()
    rig.documented_receipt("claude")
    rig.fake.set_routes(rig.repo.routes())
    monkeypatch.setenv("LEDGER_DB", str(rig.db))
    monkeypatch.setattr(cli, "LedgerRecords", partial(LedgerRecords, plugin_check=rig.plugin_check))
    monkeypatch.setattr(cli, "GhTransport", lambda executable: rig.fake.transport())
    monkeypatch.setattr(cli.StoreCheck, "shipped", lambda: rig.store.check)
    with open_ledger(rig.db, create=False) as ledger:
        before = ledger.events()
    assert cli.main(["check", *rig.repo.argv()[1:]]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"would-merge {rig.repo.url} head={rig.repo.head}\n"
    assert captured.err == ""
    with open_ledger(rig.db, create=False) as ledger:
        after = ledger.events()
    assert after == before, "check must not append any ledger event"
    assert rig.merge_calls() == []


def test_supplied_ci_stage_runs_in_stage_eight(rig):
    rig.repo.pr_runs = []  # The shipped CI stage would refuse.
    seen = []

    def supplied(current):
        seen.append((current.stage, current.merge_called))
        current.expected = {".github/workflows/ci.yml": "CI"}

    result = invoke(rig, ci_stage=supplied)
    assert result.code == 0, result.err
    assert seen == [("ci", False)]
    assert result.out == f"would-merge {rig.repo.url} head={rig.repo.head}\n"
    assert rig.merge_calls() == []


def test_supplied_ci_stage_without_selection_refuses_like_merge(tmp_path):
    merged, checked = Rig(tmp_path / "merge"), Rig(tmp_path / "check")
    empty = lambda current: None
    expected = invoke(merged, verb="merge", ci_stage=empty)
    actual = invoke(checked, ci_stage=empty)
    assert actual.code == expected.code == 2
    assert actual.reason == "ci_stage_selection_invalid"
    assert actual.err == expected.err
    assert actual.out == ""
    assert checked.merge_calls() == []


def test_internal_error_is_refused_not_unconfirmed(rig):
    def broken(current):
        raise RuntimeError("stage exploded")

    result = invoke(rig, ci_stage=broken)
    assert result.code == 2
    assert result.err == "internal_error: stage ci: RuntimeError: stage exploded\n"
    assert result.out == ""
    assert "merge_unconfirmed" not in result.err
    assert rig.merge_calls() == []
