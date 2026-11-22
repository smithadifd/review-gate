"""The filled port decides only on real ledger events and verified documents."""
from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rig import ITEM, oid
from review_gate.records import merge_key, waiver_grant_id, exception_grant_id, review_round_grant_id
from review_gate.refusal import Refusal


def refused(name, call, *args):
    with pytest.raises(Refusal) as exc:
        call(*args)
    assert exc.value.name == name, exc.value.line()


def test_real_merge_records_intent_done_then_closes(ledger_rig):
    r = ledger_rig
    r.pair()
    result = r.run()
    assert result.code == 0, result.err
    from ledger import open_ledger
    with open_ledger(r.db, create=False) as lg:
        records = [e for e in lg.events() if e.kind.startswith("review.merge-")]
        assert [e.kind for e in records] == ["review.merge-intent", "review.merge-done"]
        assert set(records[1].payload) == {"key", "pr", "head"}
        assert lg.item(ITEM)["status"] == "closed"
    assert len(r.merge_calls()) == 1


def test_paths_open_nothing_and_missing_database_stays_missing(ledger_rig, monkeypatch):
    r = ledger_rig
    from ledger import resolve_db_path
    from ledger.verbs.doc import document_root
    from review_gate.ledger_records import LedgerRecords
    path = r.db.parent / "missing.db"
    p = LedgerRecords(str(path), plugin_check=r.plugin_check)
    paths = p.database_paths()
    assert paths.database == str(resolve_db_path(path))
    assert paths.documents == document_root(resolve_db_path(path)).path
    assert not path.exists(), "database_paths created the database before admission"
    r.port = p
    result = r.run()
    assert result.reason == "receipt_store_database_missing", result.err
    assert not path.exists(), "stage 1 created a missing database"
    assert r.fake.calls() == []


def test_relative_database_refuses_before_resolution(ledger_rig):
    from review_gate.ledger_records import LedgerRecords
    refused("ledger_db_relative", LedgerRecords("ledger.db").database_paths)


def test_pr_item_missing_ambiguous_and_current_parent(ledger_rig):
    r = ledger_rig
    from ledger import open_ledger
    missing = replace(r.subject, number=8, url=r.repo.url.replace("/7", "/8"))
    refused("pr_item_missing", r.port.item, missing)
    r.append("item.created", {"type": "ticket", "title": "Parent"}, item="it-parent")
    r.append("item.linked", {"rel": "child-of", "target": "it-parent"})
    assert r.port.item(replace(r.subject, items=("it-parent",))) == ITEM
    r.append("item.created", {"type": "ticket", "title": "Other"}, item="it-other")
    r.append("item.linked", {"rel": "child-of", "target": "it-other", "moved_from": "it-parent"})
    refused("pr_item_not_in_items", r.port.item, replace(r.subject, items=("it-parent",)))
    refused("close_item_missing", r.port.item, replace(r.subject, items=("it-missing",)))
    r.append("item.linked", {"rel": "pr", "target": "OCTO/WIDGET#7"}, item="it-other")
    refused("pr_item_ambiguous", r.port.item, r.subject)


@pytest.mark.parametrize("field,value", [("head", oid("other")), ("repo", "other/widget"), ("pr", 8)])
def test_receipts_bind_repo_number_head(ledger_rig, field, value):
    r = ledger_rig
    if field != "head":
        target = f"https://github.com/{value if field == 'repo' else r.repo.slug}/pull/{value if field == 'pr' else 7}"
        r.append("item.linked", {"rel": "pr", "target": target})
    r.pair(**{field: value})
    refused("receipts_missing", r.port.receipts, r.subject, ITEM)


def test_files_digest_is_not_waivable(ledger_rig):
    r = ledger_rig
    r.pair(changed_files_sha256="f" * 64)
    refused("receipt_files_mismatch", r.port.receipts, r.subject, ITEM)


def test_two_distinct_lanes_required(ledger_rig):
    r = ledger_rig
    first = r.receipt()
    r.receipt(round_=2, prior_receipt=first.id)
    refused("receipts_insufficient", r.port.receipts, r.subject, ITEM)


@pytest.mark.parametrize("verdict", ["crashed", "unparseable"])
def test_latest_lane_round_never_falls_back(ledger_rig, verdict):
    r = ledger_rig
    first, _ = r.pair()
    r.receipt(round_=2, prior_receipt=first.id, verdict=verdict)
    refused("receipts_insufficient", r.port.receipts, r.subject, ITEM)


def test_changes_requested_empty_is_contradictory(ledger_rig):
    r = ledger_rig
    r.pair(verdict="changes_requested")
    refused("receipt_changes_requested_empty", r.port.receipts, r.subject, ITEM)


def test_family_is_derived_not_trusted(ledger_rig):
    r = ledger_rig
    r.receipt(family="anthropic")
    r.receipt("claude")
    refused("receipt_family_mismatch", r.port.receipts, r.subject, ITEM)


def test_second_family_exception_is_head_bound(ledger_rig):
    r = ledger_rig
    r.receipt("one")
    r.receipt("two")
    (r.db.parent / "config.json").write_text('{"review_second_family":"anthropic"}')
    r.grant(exception_grant_id(r.subject.repo, 7, oid("old")))
    refused("second_family_missing", r.port.receipts, r.subject, ITEM)
    election = r.grant(exception_grant_id(r.subject.repo, 7, r.subject.head))
    assert str(election) in repr(r.port.receipts(r.subject, ITEM))


@pytest.mark.parametrize("text", ['{', '[]', '{"review_second_family":"unknown"}', '{"review_second_family":null}'])
def test_bad_second_family_config_refuses(ledger_rig, text):
    r = ledger_rig
    r.pair()
    (r.db.parent / "config.json").write_text(text)
    refused("second_family_config_invalid", r.port.receipts, r.subject, ITEM)


def test_second_family_config_symlink_refuses(ledger_rig):
    r = ledger_rig
    r.pair()
    target = r.db.parent / "target.json"
    target.write_text('{}')
    (r.db.parent / "config.json").symlink_to(target)
    refused("second_family_config_unreadable", r.port.receipts, r.subject, ITEM)


def test_each_block_needs_its_own_waiver(ledger_rig):
    r = ledger_rig
    r.receipt(findings=[{"id": "f.py:1#x", "cell": "block"}, {"id": "f.py:2#y", "cell": "block"}])
    r.receipt("claude")
    r.grant(waiver_grant_id(r.subject.repo, 7, "f.py:1#x"))
    refused("open_bug_unwaived", r.port.open_bugs, r.subject, ITEM)
    r.grant(waiver_grant_id(r.subject.repo, 7, "f.py:2#y"))
    assert r.port.open_bugs(r.subject, ITEM)


@pytest.mark.parametrize("cell", ["block", "harden"])
def test_carried_root_is_exact(ledger_rig, cell):
    r = ledger_rig
    first = r.receipt(findings=[{"id": "f.py:1#prefix-root", "cell": cell}])
    r.receipt(round_=2, prior_receipt=first.id,
              findings=[{"id": "g.py:1#root", "cell": cell, "carried_from": first.id}])
    r.receipt("claude")
    if cell == "block":
        r.grant(waiver_grant_id(r.subject.repo, 7, "f.py:1#prefix-root"))
    else:
        r.append("item.created", {"type": "ticket", "title": "Follow-up",
                 "source": f"{ITEM}:harden:f.py:1#prefix-root"}, item="it-followup")
        r.append("item.linked", {"rel": "child-of", "target": ITEM}, item="it-followup")
    refused("receipt_carried_root_missing", getattr(r.port, "open_bugs" if cell == "block" else "hardening"), r.subject, ITEM)


def test_hardening_needs_exact_current_child_not_closing_item(ledger_rig):
    r = ledger_rig
    r.receipt(findings=[{"id": "f.py:1#x", "cell": "harden"}])
    r.receipt("claude")
    source = f"{ITEM}:harden:f.py:1#x"
    r.append("item.created", {"type": "ticket", "title": "Follow-up", "source": source}, item="it-followup")
    r.append("item.linked", {"rel": "child-of", "target": ITEM}, item="it-followup")
    assert r.port.hardening(r.subject, ITEM)
    refused("hardening_missing", r.port.hardening, replace(r.subject, items=(ITEM, "it-followup")), ITEM)


def test_round_cap_covers_earlier_heads_and_exact_pr(ledger_rig):
    r = ledger_rig
    prior = None
    for n in range(1, 5):
        kwargs = {} if prior is None else {"prior_receipt": prior.id}
        prior = r.receipt(round_=n, head=oid("old"), **kwargs)
    r.pair()
    r.grant(review_round_grant_id(r.subject.repo, 70, 4))
    refused("review_round_not_granted", r.port.round_cap, r.subject, ITEM)
    r.grant(review_round_grant_id(r.subject.repo, 7, 5))
    assert r.port.round_cap(r.subject, ITEM)


def test_a_lower_round_grant_never_lifts_a_later_round(ledger_rig):
    # Five rounds in one lane and only an r4 grant: round 5 stays past the cap.
    r = ledger_rig
    prior = None
    for n in range(1, 6):
        prior = r.receipt(round_=n, **({} if prior is None else {"prior_receipt": prior.id}))
    r.grant(review_round_grant_id(r.subject.repo, 7, 4))
    with pytest.raises(Refusal) as exc:
        r.port.round_cap(r.subject, ITEM)
    assert (exc.value.name, exc.value.detail) == ("review_round_not_granted", "5"), exc.value.line()
    r.grant(review_round_grant_id(r.subject.repo, 7, 5))
    assert r.port.round_cap(r.subject, ITEM)


def test_second_intent_never_authorizes_second_merge(ledger_rig):
    r = ledger_rig
    key = merge_key(r.subject.repo, 7, r.subject.head)
    assert r.port.merge_state(ITEM, key) == "absent"
    r.port.write_intent(ITEM, key)
    assert r.port.merge_state(ITEM, key) == "started"
    refused("merge_intent_unreconciled", r.port.write_intent, ITEM, key)
    assert r.merge_calls() == []


def test_recovery_never_merges_and_finishes_then_retries(ledger_rig):
    r = ledger_rig
    key = merge_key(r.subject.repo, 7, r.subject.head)
    r.port.write_intent(ITEM, key)
    assert r.run(recovery=True).code == 0
    assert r.run(recovery=True).code == 0
    assert r.port.merge_state(ITEM, key) == "finished"
    assert r.merge_calls() == []


def test_recovery_without_intent_refuses(ledger_rig):
    r = ledger_rig
    result = r.run(recovery=True)
    assert result.reason == "merge_intent_missing", result.err
    assert r.merge_calls() == []


def test_intent_is_visible_to_merge_process_before_call(ledger_rig):
    r = ledger_rig
    r.pair()
    proof = r.db.parent / "process-proof.json"
    code = (
        "import sqlite3\n"
        "if sys.argv[1:3] == ['pr', 'merge']:\n"
        f"    c = sqlite3.connect({str(r.db)!r})\n"
        "    kinds = [x[0] for x in c.execute(\"SELECT kind FROM events WHERE kind LIKE 'review.merge-%' ORDER BY id\")]\n"
        f"    open({str(proof)!r}, 'w').write(json.dumps(kinds))\n"
        "    c.close()\n"
    )
    text = r.fake.path.read_text()
    r.fake.path.write_text(text.replace("argv = sys.argv[1:]", code + "\nargv = sys.argv[1:]"))
    result = r.run()
    assert result.code == 0, result.err
    assert json.loads(proof.read_text()) == ["review.merge-intent"], "merge process did not see committed intent"


def test_grant_withdrawal_between_passes_is_live(ledger_rig):
    r = ledger_rig
    r.receipt(findings=[{"id": "f.py:1#x", "cell": "block"}])
    r.receipt("claude")
    grant_id = waiver_grant_id(r.subject.repo, 7, "f.py:1#x")
    r.grant(grant_id)
    transport = r.fake.transport()

    class WithdrawingGh:
        def run(self, argv):
            if argv[:2] == ["api", f"repos/{r.repo.slug}/git/ref/heads/main"]:
                r.append("decision.elected", {"question": "Withdraw", "chosen": "withdrawn",
                    "chosen_description": "Withdraw", "rejected": ["granted"], "scope": "test",
                    "quote_ref": 1, "quotes": ["withdraw"], "question_id": grant_id})
            return transport.run(argv)

    result = r.run(gh=WithdrawingGh())
    assert result.reason == "open_bug_unwaived", result.err
    assert r.merge_calls() == []


def test_superseding_evidence_changes_recheck_token(ledger_rig):
    r = ledger_rig
    first, _ = r.pair()
    before = r.port.receipts(r.subject, ITEM)
    transport = r.fake.transport()
    changed = False

    class ChangingGh:
        def run(self, argv):
            nonlocal changed
            if not changed and argv[:2] == ["api", f"repos/{r.repo.slug}/git/ref/heads/main"]:
                changed = True
                r.receipt(round_=2, prior_receipt=first.id)
            return transport.run(argv)

    result = r.run(gh=ChangingGh())
    assert result.reason == "records_moved_before_merge", result.err
    assert r.merge_calls() == []
    assert r.port.receipts(r.subject, ITEM) != before, "superseding evidence must change the receipts token"


def corrupt_event(r, event_id, *, payload=None, version=None, kind=None):
    """Damage a scratch database after the real writer; never fabricate a receipt."""
    import sqlite3
    with sqlite3.connect(r.db) as conn:
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'").fetchall()
        for (name,) in triggers:
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        if payload is not None:
            conn.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), event_id))
        if version is not None:
            conn.execute("UPDATE events SET payload_version=? WHERE id=?", (version, event_id))
        if kind is not None:
            conn.execute("UPDATE events SET kind=? WHERE id=?", (kind, event_id))


@pytest.mark.parametrize("shape", ["settled", "adjudicated", "cell"])
def test_settlement_shapes_refuse_without_document_parsing(ledger_rig, shape):
    r = ledger_rig
    first, _ = r.pair()
    payload = dict(first.payload)
    if shape == "cell":
        payload["findings"] = [{"id": "f.py:1#x", "cell": "settled"}]
    else:
        payload[shape] = True
    corrupt_event(r, first.id, payload=payload)
    refused("receipt_settlement_unsupported", r.port.receipts, r.subject, ITEM)


def test_unknown_payload_version_refuses(ledger_rig):
    r = ledger_rig
    first, _ = r.pair()
    corrupt_event(r, first.id, version=2)
    refused("receipt_version_unsupported", r.port.receipts, r.subject, ITEM)


@pytest.mark.parametrize("damage", ["bytes", "path", "missing"])
def test_receipt_document_is_verified_inline(ledger_rig, damage):
    from ledger import open_ledger
    r = ledger_rig
    first, _ = r.pair()
    with open_ledger(r.db, create=False) as lg:
        document = next(e for e in lg.events(kind="doc.attached") if e.payload["sha256"] == first.payload["doc_sha256"])
    payload = dict(document.payload)
    if damage == "bytes":
        payload["content_b64"] = "e30="
        name = "receipt_doc_altered"
    elif damage == "path":
        import base64
        root = Path(str(r.db) + ".documents")
        root.mkdir()
        (root / "receipt.json").write_bytes(base64.b64decode(payload.pop("content_b64")))
        payload["path"] = "receipt.json"
        name = "receipt_doc_not_inline"
    else:
        payload["role"] = "other"
        name = "receipt_doc_missing"
    corrupt_event(r, document.id, payload=payload)
    refused(name, r.port.receipts, r.subject, ITEM)


def test_informal_review_and_document_are_not_receipts(ledger_rig):
    r = ledger_rig
    r.append("review.recorded", {"round": 1, "reviewer": "lane", "verdict": "pass"})
    r.cli("doc", "add", ITEM, "-", "--role", "review-receipt", stdin={"verdict": "approved"})
    refused("receipts_missing", r.port.receipts, r.subject, ITEM)
    first, second = r.pair()
    corrupt_event(r, first.id, kind="review.receipt-forged")
    corrupt_event(r, second.id, kind="review.receipt-forged")
    refused("receipts_missing", r.port.receipts, r.subject, ITEM)


def test_generic_cli_cannot_write_receipt(ledger_rig):
    r = ledger_rig
    first = r.receipt()
    env = dict(os.environ, PYTHONPATH=str(r.plugin), LEDGER_HARNESS="pytest")
    done = subprocess.run([sys.executable, "-m", "ledger", "--json", "--db", str(r.db),
        "append", "--kind", "review.receipt", "--item", ITEM, "--payload", json.dumps(first.payload)],
        cwd=r.db.parent, env=env, text=True, capture_output=True)
    assert done.returncode != 0
    from ledger import open_ledger
    with open_ledger(r.db, create=False) as lg:
        assert [e.id for e in lg.events(kind="review.receipt")] == [first.id]


@pytest.mark.parametrize("forgery", ["elect", "raw", "typed", "annotated"])
def test_forged_grants_reach_real_standing_and_refuse(ledger_rig, forgery):
    r = ledger_rig
    r.receipt(findings=[{"id": "f.py:1#x", "cell": "block"}])
    r.receipt("claude")
    grant_id = waiver_grant_id(r.subject.repo, 7, "f.py:1#x")
    if forgery in ("typed", "annotated"):
        r.grant(grant_id, label="Other: granted" if forgery == "typed" else "granted",
                annotations={"notes": "only docs"} if forgery == "annotated" else None, raw=True)
    else:
        asked = r.append("decision.asked", {"question_id": grant_id, "question": "Stand?",
            "scope": "test", "options": [{"label": "granted", "description": "Stand"},
                                       {"label": "withdrawn", "description": "Withdraw"}]})
        quote = r.cli("append", "--kind", "utterance.captured", "--item", ITEM,
                      "--payload", '{"text":"granted","origin":"person"}',
                      "--provenance-class", "submitted-prompt")["event"]["id"]
        payload = {"question": "Stand?", "chosen": "granted", "chosen_description": "Stand",
            "rejected": ["withdrawn"], "scope": "test", "quote_ref": quote, "quotes": ["granted"],
            "question_id": grant_id, "asked_event_id": asked.id}
        if forgery == "raw":
            r.cli("append", "--kind", "decision.elected", "--item", ITEM, "--payload", json.dumps(payload))
        else:
            r.cli("elect", ITEM, "--question", "Stand?", "--chosen", "granted",
                  "--chosen-description", "Stand", "--rejected", "withdrawn", "--scope", "test",
                  "--quote-ref", str(quote), "--quote", "granted", "--ask", str(asked.id))
    from ledger.verbs import CliError
    with pytest.raises(CliError, match="UNVERIFIED"):
        r.port.standing(grant_id)
    refused("open_bug_unwaived", r.port.open_bugs, r.subject, ITEM)


def test_withdrawn_cite_and_old_election_cannot_lift_cap(ledger_rig):
    from ledger import open_ledger
    from ledger.receipts import write_grant_cite
    r = ledger_rig
    prior = None
    for n in range(1, 5):
        prior = r.receipt(round_=n, **({} if prior is None else {"prior_receipt": prior.id}))
    r.receipt("claude")
    grant_id = "grant:review-round-past-cap"
    r.grant(grant_id)
    with open_ledger(r.db, create=False) as lg:
        write_grant_cite(lg, ITEM, repo=r.subject.repo, pr=7, round=4, scope="test")
    assert r.port.round_cap(r.subject, ITEM)
    r.grant(grant_id, label="withdrawn")
    refused("review_round_not_granted", r.port.round_cap, r.subject, ITEM)
    r.grant(grant_id)
    refused("review_round_cite_stale", r.port.round_cap, r.subject, ITEM)


def test_checkout_local_ledger_never_executes(ledger_rig, tmp_path):
    r = ledger_rig
    checkout = tmp_path / "checkout"
    (checkout / "ledger").mkdir(parents=True)
    marker = tmp_path / "imported"
    (checkout / "ledger" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('local ledger')\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[1]), str(r.plugin)]),
               LEDGER_DB=str(r.db), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-m", "review_gate", *r.repo.argv()],
                          cwd=checkout, env=env, capture_output=True, text=True)
    assert not marker.exists(), "checkout-local ledger code executed"
    assert done.returncode == 2
    assert done.stderr.startswith("receipt_store_in_temp_root:"), done.stderr


def test_database_in_git_tree_is_rejected_before_open(ledger_rig):
    r = ledger_rig
    (r.db.parent / ".git").mkdir()
    result = r.run()
    assert result.reason == "receipt_store_in_lane_root", result.err
    assert r.fake.calls() == []


@pytest.mark.parametrize("body", ["", "Waiver: f.py:1 — grant\nReview round grant: r9 — grant\nHardening: f.py:1 — ticket"])
def test_body_authority_lines_do_not_change_decision(ledger_rig, body):
    r = ledger_rig
    r.pair()
    r.repo.pull_record["body"] = body
    result = r.run()
    assert result.code == 0, result.err
    assert len(r.merge_calls()) == 1


def test_concurrent_intents_have_one_winner(ledger_rig):
    from concurrent.futures import ThreadPoolExecutor
    from review_gate.ledger_records import LedgerRecords
    r = ledger_rig
    key = merge_key(r.subject.repo, 7, r.subject.head)
    ports = [LedgerRecords(str(r.db), plugin_check=r.plugin_check),
             LedgerRecords(str(r.db), plugin_check=r.plugin_check)]
    for port in ports:
        port.database_paths()

    def start(port):
        try:
            port.write_intent(ITEM, key)
            return "created"
        except Refusal as exc:
            return exc.name

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, ports))
    assert sorted(results) == ["created", "merge_intent_unreconciled"]


@pytest.mark.parametrize("state,head", [("OPEN", None), ("MERGED", oid("other"))])
def test_recovery_needs_live_merged_head(ledger_rig, state, head):
    r = ledger_rig
    key = merge_key(r.subject.repo, 7, r.subject.head)
    r.port.write_intent(ITEM, key)
    r.repo.merged.update(state=state, merged=state == "MERGED", headRefOid=head)
    result = r.run(recovery=True)
    assert result.reason == "merge_intent_unreconciled", result.err
    assert r.port.merge_state(ITEM, key) == "started"
    assert r.merge_calls() == []


@pytest.mark.parametrize("window", ["before-intent", "intent-only", "merged", "done", "one-close"])
def test_crash_windows_reconcile_without_repeating_merge(ledger_rig, window):
    from ledger import open_ledger
    r = ledger_rig
    key = merge_key(r.subject.repo, 7, r.subject.head)
    if window != "before-intent":
        r.port.write_intent(ITEM, key)
    if window == "intent-only":
        r.repo.merged.update(state="OPEN", merged=False)
    if window in ("done", "one-close"):
        r.port.write_finished(ITEM, key, oid("merge"))
    if window == "one-close":
        r.port.close_items((ITEM,), r.subject.url, r.subject.head)
    result = r.run(recovery=True)
    if window in ("before-intent", "intent-only"):
        assert result.code == 2, result.err
    else:
        assert result.code == 0, result.err
        with open_ledger(r.db, create=False) as lg:
            assert lg.item(ITEM)["status"] == "closed"
        assert r.run(recovery=True).code == 0
    assert r.merge_calls() == []


def test_version_and_package_origin_are_required_before_database_paths(ledger_rig, tmp_path):
    from ledger_rig import plugin_check_for
    from review_gate.ledger_runtime import load_ledger
    r = ledger_rig
    checkout = tmp_path / "bad-plugin"
    (checkout / ".claude-plugin").mkdir(parents=True)
    (checkout / ".claude-plugin" / "plugin.json").write_text('{"name":"ledger-hooks","version":"0.4.0"}')
    check = plugin_check_for(r.plugin_check, checkout, r.plugin)
    refused("ledger_version_unsupported", load_ledger, check)
    (checkout / ".git").mkdir()
    refused("receipt_store_in_lane_root", load_ledger, check)
