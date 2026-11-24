"""Copyable README receipts must record through the real CLI and ledger writer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import pytest

from test_record_verb import record, receipts


README = Path(__file__).resolve().parents[1] / "README.md"
SECTION = README.read_text().split("## Writing and importing receipts\n", 1)[1].split(
    "### Walk step: record one lane result\n", 1
)[0]
EXAMPLES = re.findall(r"^```json\n(.*?)^```$", SECTION, re.MULTILINE | re.DOTALL)
assert EXAMPLES, "README must include copyable receipt JSON documents"


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: json.loads(example)["lane"])
def test_readme_receipt_example_records(ledger_rig, record, example):
    r = ledger_rig
    replacements = {
        "<owner/repo>": r.subject.repo,
        "<pr-number>": r.subject.number,
        "<reviewed-head-sha>": r.subject.head,
        "<base-sha>": r.repo.base_oid,
        "<changed-files-sha256>": r.subject.files_sha256,
        "<sol-model-id>": "gpt-x",
        "<claude-model-id>": "claude-opus-x",
        "<finding-file>": "src/app.py",
        "<nit-description>": "Clarify the return value in the docstring.",
    }
    for placeholder, value in replacements.items():
        example = example.replace(json.dumps(placeholder), json.dumps(value))
    data = example.encode("utf-8")

    done = record(data)

    assert done.code == 0, done.err
    assert done.out.startswith("written "), done.out
    assert [event.payload["doc_sha256"] for event in receipts(r)] == [
        hashlib.sha256(data).hexdigest()
    ]
