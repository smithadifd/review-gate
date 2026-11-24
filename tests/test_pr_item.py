"""Real ledger transactions for the explicit pull request item verb."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io

import pytest

from review_gate import cli
from rig import ITEM as EXISTING_ITEM, Result

URL = "https://github.com/Octo/Widget/pull/81"
CANONICAL = URL.lower()
SOURCE = "pr:" + CANONICAL
KEY = "item.created:" + SOURCE
ITEM = "it-" + hashlib.sha256(KEY.encode()).hexdigest()[:12]


def invoke(rig, url=URL, lookup=False):
    from review_gate.pr_item import run
    out, err = io.StringIO(), io.StringIO()
    code = run(["item", "--pr", url, *(["--lookup"] if lookup else [])],
               records=rig.port, gh=None, store_check=rig.store.check, out=out, err=err)
    return Result(code, out.getvalue(), err.getvalue())


def snapshot(rig):
    from ledger.store import open_ledger_readonly
    with open_ledger_readonly(rig.db) as lg:
        return lg.events()


def create_source(rig, **payload):
    from ledger import open_ledger
    with open_ledger(rig.db, create=False) as lg:
        return lg.append(kind="item.created", item_id=ITEM,
                         payload=dict(type="ticket", title=CANONICAL, source=SOURCE, **payload),
                         idempotency_key=KEY, harness="review-gate")


def test_cli_dispatches_item_before_ledger_reads(monkeypatch, capsys):
    monkeypatch.delenv("LEDGER_DB", raising=False)
    assert cli.main(["item", "--pr", URL]) == 2
    assert capsys.readouterr().err.startswith("records_unavailable:"), "item must dispatch to its own runner"


def test_create_retry_and_url_case_share_one_item(ledger_rig):
    rig = ledger_rig
    before = snapshot(rig)
    first = invoke(rig)
    assert (first.code, first.out, first.err) == (0, ITEM + "\n", "")
    after = snapshot(rig)
    added = after[len(before):]
    assert [(e.kind, e.item_id) for e in added] == [("item.created", ITEM), ("item.linked", ITEM)]
    assert added[0].payload == {"type": "ticket", "title": CANONICAL, "source": SOURCE}
    assert added[1].payload == {"rel": "pr", "target": CANONICAL}
    assert invoke(rig, CANONICAL) == first
    assert snapshot(rig) == after, "retry must write no event"


def test_retry_after_create_uses_only_url_for_title(ledger_rig):
    rig = ledger_rig
    created = create_source(rig)
    result = invoke(rig)
    assert result.code == 0, result.err
    assert result.out == ITEM + "\n"
    events = snapshot(rig)
    assert [e for e in events if e.kind == "item.created" and e.item_id == ITEM] == [created]
    assert len([e for e in events if e.kind == "item.linked" and e.item_id == ITEM]) == 1


@pytest.mark.parametrize("target", [CANONICAL, "OCTO/WIDGET#81"])
def test_existing_any_item_is_read_only(ledger_rig, target):
    rig = ledger_rig
    rig.append("item.linked", {"rel": "pr", "target": target})
    before = snapshot(rig)
    for lookup in (False, True):
        result = invoke(rig, lookup=lookup)
        assert (result.code, result.out, result.err) == (0, EXISTING_ITEM + "\n", "")
    assert snapshot(rig) == before


@pytest.mark.parametrize("lookup", [False, True])
def test_ambiguous_refuses_without_writes(ledger_rig, lookup):
    rig = ledger_rig
    rig.append("item.linked", {"rel": "pr", "target": CANONICAL})
    rig.append("item.created", {"type": "ticket", "title": "other"}, item="it-other")
    rig.append("item.linked", {"rel": "pr", "target": "OCTO/WIDGET#81"}, item="it-other")
    before = snapshot(rig)
    result = invoke(rig, lookup=lookup)
    assert result.code == 2
    assert result.err.startswith("pr_item_ambiguous:"), result.err
    assert result.out == ""
    assert snapshot(rig) == before


def test_lookup_missing_never_creates(ledger_rig):
    before = snapshot(ledger_rig)
    result = invoke(ledger_rig, lookup=True)
    assert (result.code, result.out) == (3, ""), "lookup must not fall through to creation"
    assert result.err.startswith("pr_item_missing:"), result.err
    assert snapshot(ledger_rig) == before


def test_link_precondition_checks_live_state_and_rolls_back(ledger_rig, monkeypatch):
    from ledger.store import Ledger
    rig = ledger_rig
    before = snapshot(rig)
    original = Ledger.append

    def race(lg, **kwargs):
        if kwargs["kind"] == "item.linked" and kwargs["item_id"] == ITEM:
            original(lg, kind="item.linked", item_id=EXISTING_ITEM,
                     payload={"rel": "pr", "target": "OCTO/WIDGET#81"}, idempotency_key="racing-link")
        return original(lg, **kwargs)

    monkeypatch.setattr(Ledger, "append", race)
    result = invoke(rig)
    assert result.code == 2, "in-transaction precondition must refuse a newly linked other item"
    assert result.err.startswith("pr_item_link_conflict:"), result.err
    assert snapshot(rig) == before, "failed precondition must roll back creation and link"


def test_concurrent_creators_share_item_and_link(ledger_rig):
    # Resolve/import once before worker threads; each writer opens its own connection.
    ledger_rig.port.database_paths()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(ledger_rig), range(2)))
    assert [(r.code, r.out, r.err) for r in results] == [(0, ITEM + "\n", "")] * 2
    events = [e for e in snapshot(ledger_rig) if e.item_id == ITEM]
    assert [e.kind for e in events] == ["item.created", "item.linked"]


def test_foreign_source_link_refuses_without_writes(ledger_rig):
    create_source(ledger_rig)
    ledger_rig.append("item.linked", {"rel": "pr", "target": "octo/else#1"}, item=ITEM)
    before = snapshot(ledger_rig)
    result = invoke(ledger_rig)
    assert result.code == 2
    assert result.err.startswith("pr_item_foreign_link:"), result.err
    assert snapshot(ledger_rig) == before


def test_source_collision_refuses_without_writes(ledger_rig):
    from ledger import open_ledger
    with open_ledger(ledger_rig.db, create=False) as lg:
        lg.append(kind="item.created", item_id=ITEM,
                  payload={"type": "ticket", "title": "unrelated"}, idempotency_key="unrelated")
    before = snapshot(ledger_rig)
    result = invoke(ledger_rig)
    assert result.code == 2
    assert result.err.startswith("pr_item_source_conflict:"), result.err
    assert snapshot(ledger_rig) == before


def test_lookup_opens_no_writer(ledger_rig, monkeypatch):
    from ledger.store import Ledger
    def forbidden(*args, **kwargs):
        raise AssertionError("lookup opened a write transaction")
    monkeypatch.setattr(Ledger, "transaction", forbidden)
    result = invoke(ledger_rig, lookup=True)
    assert result.code == 3, result.err


def test_invalid_url_refuses_before_admission(ledger_rig, monkeypatch):
    def forbidden(*args):
        raise AssertionError("invalid URL reached ledger admission")
    monkeypatch.setattr(ledger_rig.port, "database_paths", forbidden)
    result = invoke(ledger_rig, URL + "/")
    assert result.code == 2
    assert result.err.startswith("pr_url_invalid:"), result.err


def test_unreadable_ledger_refuses_without_creation(ledger_rig):
    ledger_rig.db.unlink()
    result = invoke(ledger_rig)
    assert result.code == 2
    assert result.out == ""
    assert not ledger_rig.db.exists()


def test_loop_managed_source_refuses_without_writes(ledger_rig):
    from ledger import open_ledger
    from ledger.loop import manage_item
    create_source(ledger_rig)
    with open_ledger(ledger_rig.db, create=False) as lg:
        manage_item(lg, ITEM, idempotency_key="manage")
    before = snapshot(ledger_rig)
    result = invoke(ledger_rig)
    assert result.code == 2
    assert result.err.startswith("pr_item_loop_managed:"), result.err
    assert snapshot(ledger_rig) == before


def test_stop_before_link_rolls_back_and_retry_finishes(ledger_rig, monkeypatch):
    from ledger.store import Ledger
    original = Ledger.append
    before = snapshot(ledger_rig)

    def stop(lg, **kwargs):
        if kwargs["kind"] == "item.linked" and kwargs["item_id"] == ITEM:
            raise RuntimeError("stop after creation")
        return original(lg, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "append", stop)
        result = invoke(ledger_rig)
    assert result.code == 2
    assert result.err == "item_failed: RuntimeError\n"
    assert snapshot(ledger_rig) == before, "a stop between appends must roll back creation"
    assert invoke(ledger_rig).out == ITEM + "\n"


def test_changed_source_title_refuses_without_writes(ledger_rig):
    from ledger import open_ledger
    with open_ledger(ledger_rig.db, create=False) as lg:
        lg.append(kind="item.created", item_id=ITEM,
                  payload={"type": "ticket", "title": "changed", "source": SOURCE},
                  idempotency_key=KEY)
    before = snapshot(ledger_rig)
    result = invoke(ledger_rig)
    assert result.code == 2
    assert result.err.startswith("pr_item_source_conflict:"), result.err
    assert snapshot(ledger_rig) == before
