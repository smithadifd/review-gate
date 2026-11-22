"""Stage 11 and the stage order: write-ahead, the head-pinned merge call and the finished record."""
from __future__ import annotations

from review_gate.gate import STAGE_ORDER
from review_gate.merge import merge_argv
from review_gate.records import merge_key

from rig import ITEM, LedgerRefusal, oid


def test_a_green_pull_request_merges_at_its_head(rig):
    result = rig.run()
    assert result.code == 0, result.err
    assert result.out == f"merged {rig.repo.url} head={rig.repo.head} merge_commit={oid('merge')}\n"
    assert result.err == ""
    assert rig.merge_calls() == [merge_argv(rig.repo.url, rig.repo.head)]


def test_the_merge_call_pins_the_argument_sha(rig):
    rig.run()
    (call,) = rig.merge_calls()
    assert call == ["pr", "merge", rig.repo.url, "--squash", "--match-head-commit", rig.repo.head]


def test_the_intent_is_written_before_the_merge_call_and_finished_after_merged_is_read(rig):
    result = rig.run()
    assert result.code == 0, result.err
    key = merge_key("octo/widget", 7, rig.repo.head)
    names = rig.port.names()
    assert names[-4:] == ["merge_state", "write_intent", "write_finished", "close_items"]
    assert rig.port.calls[-3] == ("write_intent", ITEM, key)
    assert rig.port.calls[-2] == ("write_finished", ITEM, key, oid("merge"))
    endpoints = rig.endpoints()
    merge_index = next(i for i, call in enumerate(endpoints) if call.startswith("pr merge"))
    # The confirming read follows the merge call.
    assert any("mergeCommit" in call for call in endpoints[merge_index + 1:])


def test_a_failed_intent_write_makes_no_merge_call(rig):
    rig.port.intent_error = OSError("disk full")
    result = rig.run()
    assert result.code == 2
    assert result.reason == "merge_intent_unwritten"
    assert rig.merge_calls() == []


def test_a_refused_intent_write_makes_no_merge_call(rig):
    from review_gate.refusal import Refusal

    rig.port.intent_error = Refusal("records_unavailable", "no writer")
    result = rig.run()
    assert result.code == 2
    assert result.reason == "records_unavailable"
    assert rig.merge_calls() == []


def test_finished_is_written_only_after_a_read_shows_merged_at_the_head(rig):
    rig.repo.merged = {"state": "OPEN", "merged": False, "headRefOid": None, "mergeCommit": None}
    rig.repo.merge_answer = {"stdout": "", "stderr": "merge refused\n", "exit": 1}
    result = rig.run()
    assert result.code == 4
    assert result.reason == "merge_unconfirmed"
    assert "write_finished" not in rig.port.names()
    assert "close_items" not in rig.port.names()
    assert result.out == ""


def test_merged_at_another_head_is_not_confirmed(rig):
    rig.repo.merged = {"state": "MERGED", "merged": True, "headRefOid": oid("other"), "mergeCommit": {"oid": oid("m")}}
    result = rig.run()
    assert result.code == 4
    assert result.reason == "merge_unconfirmed"
    assert "write_finished" not in rig.port.names()


def test_a_started_state_on_entry_never_issues_a_second_merge_call(rig):
    rig.port.state = "started"
    rig.repo.merged = {"state": "OPEN", "merged": False, "headRefOid": None, "mergeCommit": None}
    result = rig.run()
    assert result.code == 2
    assert result.reason == "merge_intent_unreconciled"
    assert rig.merge_calls() == []
    assert "write_intent" not in rig.port.names()


def test_a_started_state_reconciles_a_merge_github_reads_as_merged(rig):
    from review_gate import merge

    rig.port.state = "started"
    rig.fake.set_routes(rig.repo.routes())

    class Args:
        url = rig.repo.url
        sha = rig.repo.head
        items = (ITEM,)

        class pr:
            owner, repo, number, slug = "octo", "widget", 7, "octo/widget"

    import io

    class Gate:
        args = Args
        records = rig.port
        gh = rig.fake.transport()
        item = ITEM
        out = io.StringIO()
        err = io.StringIO()
        merge_called = False

    code = merge.stage_merge(Gate)
    assert code == 0
    assert rig.merge_calls() == []
    assert ("write_finished", ITEM, merge_key("octo/widget", 7, rig.repo.head), oid("merge")) in rig.port.calls
    assert Gate.out.getvalue().startswith("merged ")


def test_a_finished_state_refuses(rig):
    rig.port.state = "finished"
    result = rig.run()
    assert result.reason == "merge_already_finished"
    assert rig.merge_calls() == []


def test_an_unrecorded_finish_exits_unconfirmed(rig):
    rig.port.finished_error = LedgerRefusal("locked", "db_locked")
    result = rig.run()
    assert result.code == 4
    assert result.reason == "merge_finish_unrecorded"
    assert result.out == ""
    assert "close_items" not in rig.port.names()


def test_an_items_closure_failure_never_changes_the_merged_outcome(rig):
    def broken(items, url, head):
        raise LedgerRefusal("locked", "db_locked")

    rig.port.close_items = broken
    result = rig.run()
    assert result.code == 0
    assert result.out.startswith("merged ")
    assert result.err.startswith("items_close_failed: ")


def test_the_stages_run_in_the_fixed_order(rig):
    assert STAGE_ORDER == (
        "arguments", "store", "state_read", "item", "receipts", "open_bugs", "hardening",
        "round_cap", "ci", "main_green", "recheck", "merge",
    )
    result = rig.run()
    assert result.code == 0, result.err
    assert rig.port.names() == [
        "database_paths", "item", "receipts", "open_bugs", "hardening", "round_cap",
        "item", "receipts", "open_bugs", "hardening", "round_cap",
        "merge_state", "write_intent", "write_finished", "close_items",
    ]
    endpoints = rig.endpoints()
    first_state = 0
    assert "changedFiles" in endpoints[first_state]
    runs = next(i for i, call in enumerate(endpoints) if "actions/runs?head_sha=" in call)
    push = next(i for i, call in enumerate(endpoints) if "event=push" in call)
    base = next(i for i, call in enumerate(endpoints) if f"compare/main...{rig.repo.head}" in call)
    second_state = max(i for i, call in enumerate(endpoints) if "changedFiles" in call)
    merge_at = next(i for i, call in enumerate(endpoints) if call.startswith("pr merge"))
    assert first_state < runs < push < second_state < base < merge_at


def test_an_error_escaping_a_stage_is_a_named_refusal_not_a_traceback(rig):
    def explode(subject, item):
        raise RuntimeError("boom")

    rig.port.receipts = explode
    result = rig.run()
    assert result.code == 2
    assert result.reason == "records_read_failed"
    assert "Traceback" not in result.err
    assert rig.merge_calls() == []


def test_an_unexpected_error_is_internal_error_with_exit_2(rig, monkeypatch):
    from review_gate import gate

    def explode(_gate):
        raise KeyError("surprise")

    monkeypatch.setattr(gate, "STAGES", (("store", explode),))
    result = rig.run()
    assert result.code == 2
    assert result.reason == "internal_error"
    assert "Traceback" not in result.err


def test_a_refusal_from_the_merge_transport_exits_unconfirmed(rig, monkeypatch):
    from review_gate.gh import GhTransport
    from review_gate.refusal import Refusal

    original = GhTransport.run
    attempted = []

    def transport(self, args):
        if args[:2] == ["pr", "merge"]:
            attempted.append(args)
            raise Refusal("gh_unavailable", "transport lost after dispatch")
        return original(self, args)

    monkeypatch.setattr(GhTransport, "run", transport)
    result = rig.run()
    assert attempted == [merge_argv(rig.repo.url, rig.repo.head)]
    assert result.code == 4, "post-merge refusal must not report nothing merged"
    assert result.reason == "merge_unconfirmed"
    assert "write_finished" not in rig.port.names()


def test_an_output_error_after_merge_exits_unconfirmed(rig, monkeypatch):
    import io

    class BrokenOutput(io.StringIO):
        def write(self, text):
            if text.startswith("merged "):
                raise OSError("output unavailable")
            return super().write(text)

    monkeypatch.setattr("rig.io.StringIO", BrokenOutput)
    result = rig.run()
    assert rig.merge_calls() == [merge_argv(rig.repo.url, rig.repo.head)]
    assert result.code == 4, "post-merge exception must not report nothing merged"
    assert result.reason == "merge_unconfirmed"
