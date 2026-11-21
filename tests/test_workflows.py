"""Reading workflow triggers exactly: pull_request selection and push triggers."""
from __future__ import annotations

import pytest

from review_gate.refusal import Refusal
from review_gate.workflows import (
    NO_PUSH,
    PATH_FILTERED,
    RUNS,
    branch_filter_regex,
    ignore_match,
    load_workflow,
    pattern_match,
    push_trigger,
    selected_workflows,
)


def docs(**texts):
    return {f".github/workflows/{name}.yml": load_workflow(name, text) for name, text in texts.items()}


def test_selection_by_paths_and_branches():
    found = selected_workflows(
        docs(
            ci="name: CI\non: [push, pull_request]\n",
            docs="name: Docs\non:\n  pull_request:\n    paths: ['docs/**']\n",
            release="on:\n  pull_request:\n    branches: [release]\n",
            ignored="on:\n  pull_request:\n    paths-ignore: ['src/**']\n",
            closed="on:\n  pull_request:\n    types: [closed]\n",
            push_only="on: push\n",
        ),
        ["src/app.py"],
        "main",
    )
    assert found == {".github/workflows/ci.yml": "CI"}


def test_a_workflow_with_no_name_is_named_by_its_path():
    assert selected_workflows(docs(x="on: pull_request\n"), ["a"], "main") == {
        ".github/workflows/x.yml": ".github/workflows/x.yml"
    }


@pytest.mark.parametrize("text", [
    "on:\n  pull_request:\n    paths: ['!x']\n",
    "on:\n  pull_request:\n    paths: []\n",
    "on:\n  pull_request:\n    paths: x\n",
    "on:\n  pull_request:\n    types: [bogus]\n",
    "on:\n  pull_request:\n    paths: [a]\n    paths-ignore: [b]\n",
    "on:\n  pull_request:\n    branches: [a]\n    branches-ignore: [b]\n",
    "on:\n  pull_request:\n    branches: ['re[l]ease']\n",
    "on: 5\n",
    "jobs: {}\n",
    "- a list\n",
])
def test_an_unreadable_trigger_refuses(text):
    with pytest.raises(Refusal) as refused:
        selected_workflows(docs(x=text), ["src/app.py"], "main")
    assert refused.value.name == "workflow_paths_unreadable"


def test_yaml_the_loader_refuses_is_unreadable():
    with pytest.raises(Refusal) as refused:
        load_workflow("x", "on: [push\n")
    assert refused.value.name == "workflow_paths_unreadable"


@pytest.mark.parametrize("text,expected", [
    ("on: push\n", RUNS),
    ("on: [push]\n", RUNS),
    ("on:\n  push:\n", RUNS),
    ("on:\n  push:\n    branches: [main]\n", RUNS),
    ("on:\n  push:\n    branches: ['**']\n", RUNS),
    ("on:\n  push:\n    branches-ignore: [main]\n", NO_PUSH),
    ("on:\n  push:\n    branches: [release]\n", NO_PUSH),
    ("on:\n  push:\n    tags: ['v*']\n", NO_PUSH),
    ("on:\n  push:\n    branches: [main]\n    paths: ['src/**']\n", PATH_FILTERED),
    ("on:\n  push:\n    paths-ignore: ['docs/**']\n", PATH_FILTERED),
    ("on: pull_request\n", NO_PUSH),
    ("on:\n  workflow_dispatch:\n", NO_PUSH),
])
def test_push_triggers(text, expected):
    assert push_trigger("x", load_workflow("x", text), "main") == expected


def test_an_unreadable_push_filter_refuses():
    with pytest.raises(Refusal) as refused:
        push_trigger("x", load_workflow("x", "on:\n  push:\n    branches: main\n"), "main")
    assert refused.value.name == "workflow_paths_unreadable"


def test_patterns():
    assert pattern_match("docs/**", "docs/a/b.md")
    assert pattern_match("**/README.md", "README.md")
    assert not pattern_match("docs/*", "docs/a/b.md")
    assert ignore_match("docs/**", "docs/a.md")
    assert not ignore_match("docs/**", "docs")
    assert branch_filter_regex("feature/*").fullmatch("feature/x")
    assert not branch_filter_regex("feature/*").fullmatch("feature/x/y")
    assert branch_filter_regex("release/**/stable").fullmatch("release/stable")
    assert branch_filter_regex("a?b") is None
