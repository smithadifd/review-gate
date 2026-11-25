"""Library stage 8 substitution, selection shapes and the fixed public command."""
from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from review_gate import ci, cli, gate
from review_gate.records import GrantProof, no_ci_grant_id
from review_gate.refusal import Refusal

from rig import Result


CI = ".github/workflows/ci.yml"


def run_ci(rig, ci_stage):
    rig.fake.set_routes(rig.repo.routes())
    out, err = io.StringIO(), io.StringIO()
    code = gate.run(rig.repo.argv(), records=rig.port, gh=rig.fake.transport(),
                    store_check=rig.store.check, out=out, err=err, ci_stage=ci_stage)
    return Result(code, out.getvalue(), err.getvalue())


def spy_stages(monkeypatch):
    spies = {name: Mock(wraps=stage) for name, stage in gate.STAGES if name in ("ci", "main_green")}
    monkeypatch.setattr(gate, "STAGES", tuple((name, spies.get(name, stage)) for name, stage in gate.STAGES))
    return spies


def test_supplied_stage_replaces_ci_once_and_reaches_main_green(rig, monkeypatch):
    spies = spy_stages(monkeypatch)
    seen = []

    def supplied(current):
        seen.append(current)
        assert current.stage == "ci"
        current.expected = {CI: "CI"}

    # The shipped stage would refuse; the supplied rule deliberately does not judge these runs.
    rig.repo.pr_runs = []
    result = run_ci(rig, supplied)
    assert result.code == 0, result.err
    assert len(seen) == 1
    spies["ci"].assert_not_called()
    spies["main_green"].assert_called_once_with(seen[0])
    assert len(rig.merge_calls()) == 1


def test_no_keyword_keeps_the_shipped_success_and_refusal(rig):
    result = rig.run()
    assert result.code == 0, result.err
    rig.repo.pr_runs = []
    result = rig.run()
    assert result.code == 2
    assert result.err == (
        f"ci_no_runs: no workflow run bound to this pull request on {rig.repo.head} "
        "was created after READY (2026-11-20T10:00:00+00:00)\n"
    )


def test_empty_supplied_stage_refuses_before_main_green(rig, monkeypatch):
    spies = spy_stages(monkeypatch)
    result = run_ci(rig, lambda current: None)
    assert result.code == 2
    assert result.reason == "ci_stage_selection_invalid", result.err
    assert "none" in result.err
    spies["main_green"].assert_not_called()
    assert rig.merge_calls() == []


PROOF = GrantProof(no_ci_grant_id("octo/widget"), 31)


@pytest.mark.parametrize("expected,grant,problem", [
    ({CI: "CI"}, PROOF, "both"),
    (None, PROOF, "malformed expected"),
    ({"": "x"}, None, "malformed expected"),
    ({"a.yml": 3}, None, "malformed expected"),
    ({}, object(), "malformed no_ci_grant"),
    ({"a.yml": ""}, None, "malformed expected"),
    ({3: "CI"}, None, "malformed expected"),
    ([], None, "malformed expected"),
    (None, None, "malformed expected"),
    ({}, None, "none"),
], ids=["both", "grant-with-none", "empty-key", "non-string-value", "non-proof",
        "empty-value", "non-string-key", "non-dict", "none-expected", "neither"])
def test_invalid_selection_refuses_before_main_green(rig, monkeypatch, expected, grant, problem):
    spies = spy_stages(monkeypatch)

    def supplied(current):
        current.expected = expected
        current.no_ci_grant = grant

    result = run_ci(rig, supplied)
    assert result.code == 2
    assert result.reason == "ci_stage_selection_invalid", result.err
    assert problem in result.err
    spies["main_green"].assert_not_called()
    assert rig.merge_calls() == []


def test_shipped_stage_uses_the_same_selection_check(rig, monkeypatch):
    monkeypatch.setattr(ci, "selected_workflows", lambda *args: {CI: 3})
    monkeypatch.setattr(ci, "post_ready_runs_green", lambda *args, **kwargs: [])
    spies = spy_stages(monkeypatch)
    result = rig.run()
    assert result.reason == "ci_stage_selection_invalid", result.err
    spies["main_green"].assert_not_called()
    assert rig.merge_calls() == []


@pytest.mark.parametrize("live_event,reason", [(31, ""), (32, "grant_moved_before_merge")])
def test_supplied_grant_is_rechecked_against_live_records(rig, live_event, reason):
    rig.repo.head_workflows = {}
    rig.repo.tip_workflows = {}
    rig.port.grants[PROOF.grant_id] = live_event

    def supplied(current):
        current.expected = {}
        current.no_ci_grant = PROOF

    result = run_ci(rig, supplied)
    assert result.reason == reason, result.err
    assert result.code == (2 if reason else 0), result.err
    assert rig.port.names().count("standing") == 1
    assert len(rig.merge_calls()) == (0 if reason else 1)


@pytest.mark.parametrize("argv", [
    ["merge"], ["land"], [],
    ["merge", "https://github.com/octo/widget/pull/7", "a" * 40, "--items", "it-1"],
])
def test_cli_never_forwards_ci_stage(monkeypatch, argv):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    spy = Mock(return_value=19)
    monkeypatch.setattr(cli, "run", spy)
    assert cli.main(argv) == 19
    spy.assert_called_once()
    assert spy.call_args.args == (argv,)
    assert "ci_stage" not in spy.call_args.kwargs


@pytest.mark.parametrize("extra", [
    ["--ci-stage=x"], ["--ci-stage", "x"], ["--CI-stage=x"], ["--ci-stage="],
])
def test_cli_ci_stage_tokens_are_unknown(rig, monkeypatch, capsys, extra):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    assert cli.main(rig.repo.argv(*extra)) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("argument_unknown: "), captured.err
    assert captured.out == ""


@pytest.mark.parametrize("error,prefix", [
    (Refusal("x_named", "deliberate refusal"), "x_named: deliberate refusal\n"),
    (ValueError("bad rule"), "internal_error: stage ci: ValueError: bad rule\n"),
])
def test_supplied_stage_errors_keep_ci_stage_identity(rig, monkeypatch, error, prefix):
    spies = spy_stages(monkeypatch)

    def supplied(current):
        raise error

    result = run_ci(rig, supplied)
    assert result.code == 2
    assert result.err == prefix
    spies["main_green"].assert_not_called()
    assert rig.merge_calls() == []


@pytest.mark.parametrize("supplied", [None, object(), 3])
def test_noncallable_stage_refuses_inside_ci_slot(rig, monkeypatch, supplied):
    spies = spy_stages(monkeypatch)
    result = run_ci(rig, supplied)
    assert result.code == 2
    assert result.err.startswith("internal_error: stage ci: TypeError: "), result.err
    spies["main_green"].assert_not_called()
    assert rig.merge_calls() == []
