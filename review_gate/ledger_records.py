"""The records port for the accepted ledger versions (ledger_runtime.SUPPORTED_VERSIONS). Each call opens a fresh connection; no gate snapshot.

Receipt authority is the reserved event, not JSON in its verified document.
Settlement-bearing events are unsupported until document-derived settlement
validation exists. Stage tokens retain event identities and grant elections.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import stat

from .carried_root import carried_origin
from .ledger_runtime import load_ledger, ledger_module
from .records import DatabasePaths, ask_grant, exception_grant_id, merge_key, review_round_grant_id, waiver_grant_id
from .refusal import Refusal, refuse

# Anchored provider/model forms, not substrings; unknown models have no family.
FAMILY_MODELS = (
    (re.compile(r"(?:anthropic/)?claude-[a-z0-9][a-z0-9._-]*"), "anthropic"),
    (re.compile(r"(?:openai(?:-codex)?/)?(?:gpt-[a-z0-9][a-z0-9._-]*|o[1-9](?:-[a-z0-9._-]+)?)"), "openai"),
    (re.compile(r"(?:google(?:-gemini-cli)?/)?gemini-[a-z0-9][a-z0-9._-]*"), "google"),
)


def family(model):
    for pattern, value in FAMILY_MODELS:
        if isinstance(model, str) and pattern.fullmatch(model):
            return value
    return None


def _identity(event):
    # IDs identify immutable evidence; payload equality also detects an edited DB.
    return (event.id, event.kind, event.payload_version, json.dumps(event.payload, sort_keys=True))


class LedgerRecords:
    def __init__(self, database, *, plugin_check=None):
        # plugin_check: the StoreCheck the plugin root must pass; None is the shipped one.
        self.given = database
        self.plugin_check = plugin_check
        self.paths = None
        self.ledger = None

    def database_paths(self):
        if self.paths is None:
            if not isinstance(self.given, str) or not Path(self.given).is_absolute():
                refuse("ledger_db_relative", "LEDGER_DB must name an absolute database file")
            self.ledger = load_ledger(self.plugin_check)
            try:
                database = self.ledger.resolve_db_path(self.given)
            except Exception as exc:
                refuse("ledger_db_invalid", type(exc).__name__)
            self.root = ledger_module("verbs.doc").document_root(database)
            self.paths = DatabasePaths(str(database), self.root.path)
        return self.paths

    @contextmanager
    def _open(self, *, write=False):
        paths = self.database_paths()
        try:
            store = ledger_module("store")
            with (store.open_ledger(paths.database, create=False) if write
                  else store.open_ledger_readonly(paths.database)) as lg:
                yield lg
        except ledger_module("verbs").CliError as exc:
            refuse(exc.code, str(exc))

    @staticmethod
    def _parent(lg, item):
        links = [e for e in lg.events(item_id=item, kind="item.linked")
                 if e.payload["rel"] == "child-of"]
        return links[-1].payload["target"] if links else None

    def item(self, subject):
        with self._open() as lg:
            item = ledger_module("receipts").pr_item(lg, subject.url)
            for closing in subject.items:
                if lg.item(closing) is None:
                    refuse("close_item_missing", closing)
            if item not in subject.items and self._parent(lg, item) not in subject.items:
                refuse("pr_item_not_in_items", item)
            return item

    def standing(self, grant_id):
        # Keep CliError's code for ask_grant, which maps non-standing answers.
        self.database_paths()
        with ledger_module("store").open_ledger_readonly(self.paths.database) as lg:
            return ledger_module("verbs.grants").standing(lg, grant_id)

    def _second_family(self):
        path = Path(self.paths.database).parent / "config.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            refuse("second_family_config_unreadable", "config.json cannot be opened without following a symlink")
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                refuse("second_family_config_unreadable", "config.json is not a regular file")
            with os.fdopen(fd, encoding="utf-8") as handle:
                fd = None
                value = json.load(handle)
        except (OSError, ValueError, UnicodeError):
            refuse("second_family_config_invalid", "config.json must be a JSON object")
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(value, dict):
            refuse("second_family_config_invalid", "config.json must be a JSON object")
        if "review_second_family" not in value:
            return None
        required = value["review_second_family"]
        if required not in ("anthropic", "openai", "google"):
            refuse("second_family_config_invalid", "review_second_family must name a known model family")
        return required

    def _events(self, lg, subject, item, *, current=True):
        result = []
        doc = ledger_module("verbs.doc")
        documents = None
        for event in lg.events(item_id=item):
            if event.kind != "review.receipt":
                continue
            p = event.payload
            if (p.get("repo"), p.get("pr")) != (subject.repo, subject.number):
                continue
            if current and p.get("head") != subject.head:
                continue
            if event.payload_version != 1:
                refuse("receipt_version_unsupported", str(event.id))
            # Future or damaged settlement shapes never silently become approvals.
            if (p.get("verdict") in ("settled", "adjudicated")
                    or p.get("settled") or p.get("adjudicated")
                    or any(f.get("cell") == "settled" for f in p.get("findings", ()))):
                refuse("receipt_settlement_unsupported", str(event.id))
            try:
                ledger_module("schema").validate_payload(event.kind, p, payload_version=event.payload_version)
            except self.ledger.ValidationError:
                refuse("receipt_shape_invalid", str(event.id))
            if documents is None:
                documents = doc.item_documents(lg, item)
            matches = [d for d in documents if d.payload.get("role") == "review-receipt"
                       and d.payload.get("sha256") == p["doc_sha256"]]
            if not matches:
                refuse("receipt_doc_missing", str(event.id))
            document = matches[-1]
            if "path" in document.payload:
                refuse("receipt_doc_not_inline", str(event.id))
            try:
                doc.verified_content(document, self.root)
            except ledger_module("verbs").CliError:
                refuse("receipt_doc_altered", str(event.id))
            if current and p["changed_files_sha256"] != subject.files_sha256:
                refuse("receipt_files_mismatch", str(event.id))
            derived = family(p.get("served_model"))
            if derived is not None and p.get("family") != derived:
                refuse("receipt_family_mismatch", str(event.id))
            if p["verdict"] == "changes_requested" and not p["findings"]:
                refuse("receipt_changes_requested_empty", str(event.id))
            result.append(event)
        return result

    def _deciding(self, lg, subject, item):
        events = self._events(lg, subject, item)
        if not events:
            refuse("receipts_missing", subject.url)
        newest = {}
        for event in events:
            p = event.payload
            slot = (p["lane"], p["round"], p["head"])
            prior = newest.get(slot)
            if (prior is None and "supersedes" in p) or (prior is not None and p.get("supersedes") != prior.id):
                refuse("receipt_supersedes_invalid", str(event.id))
            newest[slot] = event
        lanes = {}
        for event in newest.values():
            lane = event.payload["lane"]
            if lane not in lanes or event.payload["round"] > lanes[lane].payload["round"]:
                lanes[lane] = event
        deciding_slots = {(e.payload["lane"], e.payload["round"], e.payload["head"])
                          for e in lanes.values()}
        for event in events:
            p = event.payload
            if (p["lane"], p["round"], p["head"]) in deciding_slots and "supersedes" in p:
                refuse("receipt_settlement_unsupported", str(event.id))
        counted = tuple(e for e in lanes.values() if e.payload["verdict"] in ("approved", "changes_requested"))
        if len(counted) < 2:
            refuse("receipts_insufficient", "two distinct deciding lanes are required")
        return counted

    def receipts(self, subject, item):
        with self._open() as lg:
            counted = self._deciding(lg, subject, item)
            required = self._second_family()
            proof = None
            if required is not None and required not in {family(e.payload.get("served_model")) for e in counted}:
                proof = ask_grant(self, exception_grant_id(subject.repo, subject.number, subject.head),
                                  "second_family_missing", "receipts")
            return (tuple(_identity(e) for e in counted), required, proof)

    def _root_finding(self, lg, subject, item, event, finding):
        # The origin id (the waiver id and the hardening source) and the events walked to it.
        if "carried_from" not in finding:
            return finding["id"], ()
        origin, visited = carried_origin(finding, self._events(lg, subject, item, current=False))
        return origin, tuple(_identity(e) for e in visited)

    def open_bugs(self, subject, item):
        with self._open() as lg:
            counted = self._deciding(lg, subject, item)
            proofs = []
            for event in counted:
                for finding in event.payload["findings"]:
                    if finding["cell"] != "block":
                        continue
                    finding_id, history = self._root_finding(lg, subject, item, event, finding)
                    proof = ask_grant(self, waiver_grant_id(subject.repo, subject.number, finding_id),
                                      "open_bug_unwaived", finding_id)
                    proofs.append((finding_id, history, proof))
            return (tuple(_identity(e) for e in counted), tuple(proofs))

    def hardening(self, subject, item):
        with self._open() as lg:
            counted = self._deciding(lg, subject, item)
            evidence = []
            for event in counted:
                for finding in event.payload["findings"]:
                    if finding["cell"] != "harden":
                        continue
                    finding_id, history = self._root_finding(lg, subject, item, event, finding)
                    source = f"{item}:harden:{finding_id}"
                    matches = []
                    for row in lg.items():
                        child = row["item_id"]
                        if child == item or child in subject.items:
                            continue
                        created = lg.events(item_id=child, kind="item.created")[0]
                        if created.payload.get("source") == source and self._parent(lg, child) == item:
                            matches.append((child, tuple(_identity(e) for e in lg.events(item_id=child))))
                    if not matches:
                        refuse("hardening_missing", finding_id)
                    evidence.append((finding_id, history, tuple(matches)))
            return (tuple(_identity(e) for e in counted), tuple(evidence))

    def round_cap(self, subject, item):
        with self._open() as lg:
            events = self._events(lg, subject, item, current=False)
            rounds = sorted({e.payload["round"] for e in events if e.payload["counts_toward_cap"]
                             and e.payload["verdict"] in ("approved", "changes_requested") and e.payload["round"] > 3})
            evidence = []
            elections = lg.events(kind="decision.elected")
            cites = lg.events(item_id=item, kind="review.grant-cite")
            for round_ in rounds:
                candidates = {round_}
                for election in elections:
                    grant_id = election.payload.get("question_id", "")
                    match = re.fullmatch(r"grant:review-round:([a-z0-9_.-]+/[a-z0-9_.-]+)#([1-9][0-9]*):r([1-9][0-9]*)", grant_id)
                    if match and (match[1], int(match[2])) == (subject.repo, subject.number) and int(match[3]) >= round_:
                        candidates.add(int(match[3]))
                proof = None
                for candidate in sorted(candidates):
                    try:
                        proof = ask_grant(self, review_round_grant_id(subject.repo, subject.number, candidate),
                                          "review_round_not_granted", str(round_))
                        break
                    except Refusal as exc:
                        if exc.name != "review_round_not_granted":
                            raise
                if proof is not None:
                    evidence.append((round_, proof))
                    continue
                matches = [e for e in cites if (e.payload.get("repo"), e.payload.get("pr"), e.payload.get("round"))
                           == (subject.repo, subject.number, round_)]
                if not matches:
                    refuse("review_round_not_granted", str(round_))
                proof = ask_grant(self, ledger_module("verbs.grants").REVIEW_ROUND_GRANT_ID,
                                  "review_round_not_granted", str(round_))
                valid = [e for e in matches if e.payload.get("grant_id") == proof.grant_id
                         and e.payload.get("grant_event_id") == proof.deciding_event_id and e.payload_version == 1]
                if not valid:
                    refuse("review_round_cite_stale", str(round_))
                evidence.append((round_, _identity(valid[-1]), proof))
            return (tuple(_identity(e) for e in events), tuple(evidence))

    def _merge_fields(self, key):
        pattern = ledger_module("schema")._MERGE_FIELDS["key"]["pattern"]
        if not isinstance(key, str) or re.fullmatch(pattern, key) is None:
            refuse("merge_key_invalid", "expected a full merge key")
        target, head = key[len("merge:"):].rsplit(":", 1)
        repo, number = target.rsplit("#", 1)
        if merge_key(repo, int(number), head) != key:
            refuse("merge_key_invalid", "noncanonical merge key")
        return {"key": key, "pr": ledger_module("receipts").pr_url(repo, int(number)), "head": head}

    def merge_state(self, item, key):
        self.database_paths()
        fields = self._merge_fields(key)
        with self._open() as lg:
            state = "absent"
            for event in lg.events(item_id=item):
                if event.kind not in ("review.merge-intent", "review.merge-done") or event.payload.get("key") != key:
                    continue
                if event.payload_version != 1 or event.payload != fields:
                    refuse("merge_record_invalid", key)
                if event.kind == "review.merge-intent" and state == "absent":
                    state = "started"
                elif event.kind == "review.merge-done" and state == "started":
                    state = "finished"
                else:
                    refuse("merge_record_invalid", key)
            return state

    def write_intent(self, item, key):
        with self._open(write=True) as lg:
            result = ledger_module("receipts").write_merge_intent(lg, item, self._merge_fields(key), harness="review-gate")
            if not result.created or result.state != "started":
                refuse("merge_intent_unreconciled", key)

    def write_finished(self, item, key, merge_commit):
        # Every accepted ledger version stores only key/pr/head; GitHub and stdout retain the merge commit.
        with self._open(write=True) as lg:
            result = ledger_module("receipts").write_merge_done(lg, item, self._merge_fields(key), harness="review-gate")
            if result.state != "finished":
                refuse("merge_finish_unrecorded", key)

    def close_items(self, items, url, head):
        with self._open(write=True) as lg:
            for item in items:
                row = lg.item(item)
                if row is None:
                    refuse("close_item_missing", item)
                if row["status"] != "closed":
                    lg.append(kind="item.state", item_id=item, payload={"status": "closed"},
                              idempotency_key=f"review-gate:close:{item}:{url}:{head}", harness="review-gate")
