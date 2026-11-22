"""Stage 0 checks every argument before reads; LEDGER_DB selects only the database."""
from __future__ import annotations

import io
import os

import pytest

from review_gate import cli, gate
from review_gate.grammar import parse_merge_args
from review_gate.records import RECORDS_METHODS, RecordsPort, UnfilledRecords
from review_gate.refusal import Refusal

from rig import ITEM, FakeGh, Repo, oid

URL = Repo().url
SHA = Repo().head


class CountingStore:
    def __init__(self):
        self.calls = 0

    def check(self, paths):
        self.calls += 1
        raise AssertionError("stage 1 ran")


CASES = [
    # The URL.
    (["merge", URL + "/", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL + "/files", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL.replace("https://", "http://"), SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL.replace("github.com", "www.github.com"), SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL + "?tab=files", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL + "#top", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", "https://github.com/octo/widget/pull/0", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", "https://github.com/octo/widget/pull/07", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", "https://github.com/./widget/pull/7", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", "https://github.com/octo/../pull/7", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", URL + "\n", SHA, "--items", ITEM], "pr_url_invalid"),
    (["merge", "https://github.com/octo/wid\nget/pull/7", SHA, "--items", ITEM], "pr_url_invalid"),
    # The sha.
    (["merge", URL, SHA[:39], "--items", ITEM], "sha_invalid"),
    (["merge", URL, SHA + "0", "--items", ITEM], "sha_invalid"),
    (["merge", URL, SHA.upper(), "--items", ITEM], "sha_invalid"),
    (["merge", URL, SHA[:7], "--items", ITEM], "sha_invalid"),
    (["merge", URL, SHA + "\n", "--items", ITEM], "sha_invalid"),
    # --items.
    (["merge", URL, SHA], "items_missing"),
    (["merge", URL, SHA, "--items"], "items_value_missing"),
    (["merge", URL, SHA, "--items="], "items_value_missing"),
    (["merge", URL, SHA, "--items", "it-1,,it-2"], "items_element_empty"),
    (["merge", URL, SHA, "--items", "it-1,"], "items_element_empty"),
    (["merge", URL, SHA, "--items", "it-1,it-1"], "items_id_duplicate"),
    (["merge", URL, SHA, "--items", "it-1", "--items", "it-2"], "items_given_twice"),
    (["merge", URL, SHA, "--items", "it 1"], "items_id_invalid"),
    (["merge", URL, SHA, "--items", "-it"], "items_id_invalid"),
    # --remediation.
    (["merge", URL, SHA, "--items", ITEM, "--remediation"], "remediation_value_missing"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation="], "remediation_value_missing"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation=test,"], "remediation_element_empty"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation=test,test"], "remediation_job_duplicate"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation=a", "--remediation=b"], "remediation_given_twice"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation= test"], "remediation_job_invalid"),
    (["merge", URL, SHA, "--items", ITEM, "--remediation=te\nst"], "remediation_job_invalid"),
    # Everything else.
    (["merge", URL, SHA, "--items", ITEM, "--receipts", "/x"], "argument_unknown"),
    (["merge", URL, SHA, "--items", ITEM, "--records=ledger"], "argument_unknown"),
    (["merge", URL, SHA, "--items", ITEM, "extra"], "argument_extra"),
    (["merge", URL, "--items", ITEM], "argument_missing"),
    (["merge"], "argument_missing"),
    ([], "argument_missing"),
    (["land", URL, SHA, "--items", ITEM], "command_unknown"),
]


@pytest.mark.parametrize("argv,reason", CASES, ids=[f"{i}-{reason}" for i, (_a, reason) in enumerate(CASES)])
def test_a_bad_argument_refuses_by_name_before_any_read(tmp_path, argv, reason):
    fake = FakeGh(tmp_path / "fakegh")
    fake.set_routes(Repo().routes())

    class Port(UnfilledRecords):
        calls = []

        def __getattribute__(self, name):
            if name in RECORDS_METHODS:
                Port.calls.append(name)
            return object.__getattribute__(self, name)

    store = CountingStore()
    out, err = io.StringIO(), io.StringIO()
    code = gate.run(argv, records=Port(), gh=fake.transport(), store_check=store, out=out, err=err)
    assert code == 2
    assert err.getvalue().split(":", 1)[0] == reason, err.getvalue()
    assert err.getvalue().count("\n") == 1
    assert out.getvalue() == ""
    assert fake.calls() == []
    assert Port.calls == []
    assert store.calls == 0


def test_a_well_formed_command_parses():
    args = parse_merge_args(["merge", URL, SHA, "--items=it-1,it-2", "--remediation=lint,unit tests"])
    assert args.pr.slug == "octo/widget" and args.pr.number == 7
    assert args.sha == SHA
    assert args.items == ("it-1", "it-2")
    assert args.remediation == frozenset({"lint", "unit tests"})


def test_the_unfilled_port_refuses_every_call():
    port = UnfilledRecords()
    assert set(RECORDS_METHODS) == {name for name in dir(RecordsPort) if not name.startswith("_")}
    for name in RECORDS_METHODS:
        with pytest.raises(Refusal) as refused:
            getattr(port, name)()
        assert refused.value.name == "records_unavailable"


@pytest.mark.parametrize("database", [None, ""])
def test_the_shipped_command_never_reaches_github_or_the_merge_call(tmp_path, monkeypatch, capsys, database):
    """No database configured: refuse at stage 1 without touching GitHub."""
    if database is None:
        monkeypatch.delenv("LEDGER_DB", raising=False)
    else:
        monkeypatch.setenv("LEDGER_DB", database)
    fake = FakeGh(tmp_path / "bin")
    fake.set_routes(Repo().routes())
    monkeypatch.setenv("PATH", str(fake.directory) + os.pathsep + os.environ.get("PATH", ""))
    code = cli.main(["merge", URL, SHA, "--items", ITEM])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("records_unavailable: ")
    assert captured.out == ""
    assert fake.calls() == []


def test_the_shipped_wiring_is_the_unfilled_port(monkeypatch):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    seen = {}

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return 2

    monkeypatch.setattr(cli, "run", spy)
    cli.main(["merge", URL, SHA, "--items", ITEM])
    assert type(seen["records"]) is UnfilledRecords


def test_ledger_db_selects_records_but_never_policy_or_extra_arguments(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REVIEW_GATE_RECORDS", "anything")
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "ledger.db"))
    for extra in (["--records", "x"], ["--port=x"], ["--ledger-db", "x"]):
        code = cli.main(["merge", URL, SHA, "--items", ITEM, *extra])
        assert code == 2
        assert capsys.readouterr().err.startswith("argument_unknown: ")
    code = cli.main(["merge", URL, SHA, "--items", ITEM])
    assert code == 2
    assert not capsys.readouterr().err.startswith("records_unavailable: ")


def test_the_sha_is_compared_never_folded():
    with pytest.raises(Refusal) as refused:
        parse_merge_args(["merge", URL, oid("x").upper(), "--items", ITEM])
    assert refused.value.name == "sha_invalid"
