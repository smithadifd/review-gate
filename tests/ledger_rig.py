"""Temporary ledgers and real writers of the installed ledger; only GitHub is faked."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rig import ITEM, Repo, FakeGh, Result, make_store, write_roots
from review_gate.github import files_digest
from review_gate.ledger_runtime import SUPPORTED_VERSIONS
from review_gate.records import Subject
from review_gate.store import StoreCheck


def installed_version(package):
    """The ``__version__`` the installed ledger package declares, read as text so nothing is imported."""
    found = re.search(r'^__version__ = "([^"]+)"$', (Path(package) / "__init__.py").read_text(), re.M)
    assert found, "the installed ledger declares no __version__"
    return found.group(1)


def plugin_check_for(check, *entries):
    """``check`` with PYTHONPATH naming exactly ``entries``: the plugin loader's injected candidates."""
    return StoreCheck(check.temp_roots_file, check.sandbox_roots_file,
                      environ=dict(check.environ, PYTHONPATH=os.pathsep.join(str(e) for e in entries)),
                      gettempdir=check.gettempdir)


@pytest.fixture(scope="session")
def plugin_check(tmp_path_factory):
    # CI installs the exact tag; local runs supply that same source on PYTHONPATH.
    # Copy only into a temporary plugin root: a development checkout is not trusted.
    # The loader refuses a plugin root under a temp root, so this check names its
    # own temp and sandbox roots, as make_store does for the database.
    spec = importlib.util.find_spec("ledger")
    assert spec is not None, "install an accepted ledger version before running tests"
    source = Path(spec.origin).parent
    base = Path(os.path.realpath(tmp_path_factory.mktemp("plugin")))
    root = base / "ledger-hooks"
    root.mkdir(mode=0o700)
    shutil.copytree(source, root / "ledger", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    metadata = root / ".claude-plugin"
    metadata.mkdir()
    (metadata / "plugin.json").write_text(json.dumps({"name": "ledger-hooks", "version": installed_version(source)}))
    lane_tmp = base / "lane-tmp"
    lane_tmp.mkdir()
    home = base / "home"
    home.mkdir()
    check = StoreCheck(
        str(write_roots(base / "roots" / "temp-roots.txt", [str(lane_tmp)])),
        str(write_roots(base / "roots" / "sandbox-roots.txt", [".npm/_logs", ".claude/debug"])),
        environ={"PYTHONPATH": str(root), "TMPDIR": str(lane_tmp), "HOME": str(home)},
        gettempdir=lambda: str(lane_tmp),
    )
    from review_gate.ledger_runtime import load_ledger
    ledger = load_ledger(check)
    assert ledger.__version__ in SUPPORTED_VERSIONS
    assert ledger.__version__ == json.loads((metadata / "plugin.json").read_text())["version"]
    yield check
    shutil.rmtree(root)


@pytest.fixture(scope="session")
def plugin(plugin_check):
    return Path(plugin_check.environ["PYTHONPATH"])


class LedgerRig:
    def __init__(self, tmp_path, plugin, plugin_check):
        from ledger import open_ledger
        from review_gate.ledger_records import LedgerRecords
        self.repo = Repo()
        self.store = make_store(tmp_path)
        Path(self.store.paths.database).unlink()
        self.db = Path(self.store.paths.database)
        self.plugin = plugin
        self.plugin_check = plugin_check
        self.fake = FakeGh(tmp_path / "gh")
        self.port = LedgerRecords(str(self.db), plugin_check=plugin_check)
        with open_ledger(self.db) as lg:
            lg.append(kind="item.created", item_id=ITEM, payload={"type": "ticket", "title": "Pull request"},
                      idempotency_key="created")
            lg.append(kind="item.linked", item_id=ITEM, payload={"rel": "pr", "target": self.repo.url},
                      idempotency_key="linked")
        self.subject = Subject(self.repo.url, self.repo.slug, self.repo.number, self.repo.head,
                               self.repo.default, files_digest(["src/app.py"]), (ITEM,))
        self.serial = 0

    def append(self, kind, payload, item=ITEM):
        from ledger import open_ledger
        with open_ledger(self.db, create=False) as lg:
            return lg.append(kind=kind, item_id=item, payload=payload,
                             idempotency_key=f"fixture:{len(lg.events()) + 1}")

    def receipt(self, lane="sol", round_=1, **overrides):
        from ledger import open_ledger
        from ledger.receipts import write_receipt
        self.serial += 1
        fields = {"repo": self.subject.repo, "pr": self.subject.number, "head": self.subject.head,
                  "base": self.repo.base_oid, "round": round_, "counts_toward_cap": True,
                  "lane": lane, "tier": "full-live", "requested_model": "gpt-x",
                  "served_model": "gpt-x", "family": "openai", "verdict": "approved",
                  "open_bugs": 0, "findings": [], "changed_files_sha256": self.subject.files_sha256}
        if lane == "claude":
            fields.update(served_model="claude-opus-x", family="anthropic")
        fields.update(overrides)
        if fields["verdict"] in ("crashed", "unparseable"):
            fields.pop("findings", None)
            fields.pop("open_bugs", None)
        else:
            fields["open_bugs"] = sum(f["cell"] == "block" for f in fields["findings"])
        with open_ledger(self.db, create=False) as lg:
            return write_receipt(lg, ITEM, fields, json.dumps({"nonce": self.serial}).encode())

    def pair(self, **kwargs):
        return self.receipt("sol", **kwargs), self.receipt("claude", **kwargs)

    def writer_receipt(self, **changes):
        """Attach real runner bytes through review-gate's writer."""
        from test_receipt_writer import document, write
        return write(self, document(self, **changes))

    def documented_receipt(self, lane="sol", round_=1, **overrides):
        """Real document plus raw ledger event for merge-port-only root graph tests."""
        from ledger import open_ledger
        from ledger.receipts import write_receipt
        from review_gate.receipt_document import derive_document
        from test_receipt_writer import document
        findings = overrides.pop("findings", [])
        entries = []
        for finding in findings:
            locator, label = finding["id"].split("#")
            path, line = locator.rsplit(":", 1)
            entries.append({"file": path, "line": int(line), "description": label,
                            "cell": finding["cell"], "residual_root": document_root(label)})
        prior = overrides.pop("prior_receipt", None)
        changes = dict(lane=lane, round=round_, findings=entries,
                       served_model="claude-opus-x" if lane == "claude" else "gpt-x",
                       cells={cell: sum(f["cell"] == cell for f in findings)
                              for cell in ("block", "harden", "nit")},
                       open_bugs=sum(f["cell"] == "block" for f in findings))
        if prior is not None:
            with open_ledger(self.db, create=False) as lg:
                event = next(e for e in lg.events() if e.id == prior)
            changes["prior_receipt"] = {"sha256": event.payload["doc_sha256"]}
        data = document(self, **changes)
        fields = dict(derive_document(data).fields, base=self.repo.base_oid, **overrides)
        if prior is not None:
            fields["prior_receipt"] = prior
        for finding, raw in zip(fields["findings"], findings):
            if "carried_from" in raw:
                finding["carried_from"] = raw["carried_from"]
        with open_ledger(self.db, create=False) as lg:
            return write_receipt(lg, ITEM, fields, data)

    def overlap_receipt(self, *, carried_harden=False):
        """A block and harden on one id, collapsed by the real writer."""
        from test_receipt_runner_documents import cells, entry, first_round, residual, root, second_round
        from test_receipt_writer import write
        bug = entry("Open finding", "block")
        first = first_round(self, [bug])
        key = root("f.py", 1, "Open finding")
        current = dict(bug, line=2)
        harden = residual(current, "harden", key)
        changes = (dict(findings=[residual(current, "block", key)], hardened_carried=[harden],
                        cells=cells(block=1)) if carried_harden else
                   dict(findings=[harden], blocks_carried=[dict(current, residual_root=key)],
                        cells=cells(block=1, harden=1)))
        event = write(self, second_round(self, first, open_bugs=1, **changes))
        self.writer_receipt(lane="claude", served_model="claude-opus-x")
        return first, event

    def followup(self, origin, *, item="it-followup"):
        self.append("item.created", {"type": "ticket", "title": "Follow-up",
                    "source": f"{ITEM}:harden:{origin}"}, item=item)
        self.append("item.linked", {"rel": "child-of", "target": ITEM}, item=item)

    def cli(self, *args, stdin=None):
        env = dict(os.environ, PYTHONPATH=str(self.plugin), LEDGER_HARNESS="pytest")
        env.pop("LEDGER_DB", None)
        done = subprocess.run([sys.executable, "-m", "ledger", "--json", "--db", str(self.db), *args],
                              input=None if stdin is None else json.dumps(stdin), env=env,
                              cwd=self.db.parent, text=True, capture_output=True)
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout)["data"]

    def grant(self, grant_id, *, label="granted", annotations=None, raw=False):
        asked = self.append("decision.asked", {"question_id": grant_id, "question": "Stand this grant?",
            "scope": "This test only", "options": [
                {"label": "granted", "description": "The grant stands"},
                {"label": "withdrawn", "description": "The grant is withdrawn"}]})
        display = self.cli("ask-display", str(asked.id))["input"]["questions"]
        response = {"questions": display, "answers": {display[0]["question"]: label}}
        if annotations is not None:
            response["annotations"] = annotations
        hook = {"session_id": "fixture", "transcript_path": "/fixture/transcript.jsonl",
                "cwd": "/fixture", "hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
                "tool_use_id": f"tool-{asked.id}", "tool_input": {"questions": display},
                "tool_response": response}
        captured = self.cli("capture", "--kind", "picker", stdin=hook)["event"]
        if not raw:
            return self.cli("pick", ITEM, "--ask", str(asked.id))["election"]["id"]
        from ledger.answers import derive_election
        from ledger import open_ledger
        from dataclasses import replace
        with open_ledger(self.db, create=False) as lg:
            event = lg.events(after_id=captured["id"] - 1, limit=1)[0]
        if label != "granted":
            event = replace(event, payload=dict(event.payload, picker=dict(event.payload["picker"], chosen_position=1)))
        payload = derive_election(asked.id, asked.payload, event)
        return self.cli("append", "--kind", "decision.elected", "--item", ITEM,
                        "--payload", json.dumps(payload))["event"]["id"]

    def run(self, *, recovery=False, gh=None):
        from review_gate.gate import run
        from review_gate.recovery import run as recover
        self.fake.set_routes(self.repo.routes())
        out, err = io.StringIO(), io.StringIO()
        argv = self.repo.argv(items=",".join(self.subject.items))
        if recovery:
            argv[0] = "recover"
        code = (recover if recovery else run)(argv, records=self.port, gh=gh or self.fake.transport(),
                                               store_check=self.store.check, out=out, err=err)
        return Result(code, out.getvalue(), err.getvalue())

    def merge_calls(self):
        return [c for c in self.fake.calls() if c[:2] == ["pr", "merge"]]


def document_root(label):
    import hashlib
    return "origin.py:1@" + hashlib.sha256(label.encode()).hexdigest()[:16]


def document_id(ident):
    from review_gate.receipt_document import finding_id
    locator, label = ident.split("#")
    path, line = locator.rsplit(":", 1)
    return finding_id({"file": path, "line": int(line), "description": label,
                       "residual_root": document_root(label)})


@pytest.fixture
def ledger_rig(tmp_path, plugin, plugin_check):
    return LedgerRig(tmp_path, plugin, plugin_check)
