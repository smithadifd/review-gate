"""Reading GitHub Actions workflow files exactly, or refusing.

YAML is parsed with PyYAML, the one runtime dependency, through a safe loader
that refuses a duplicate key (PyYAML's default keeps the last value and
would hide one of two triggers). When PyYAML cannot be imported, every read
refuses ``pyyaml_missing``; no hand-written subset reader stands in for it.

``selected_workflows`` is the set of workflows a pull request into ``base``
changing ``paths`` runs, read at the head: a workflow is selected when its
``on`` names ``pull_request`` and its ``branches``/``branches-ignore``,
``types``, ``paths`` and ``paths-ignore`` filters admit the pull request.
``push_trigger`` says how a workflow at the default branch's tip runs on a
push to that branch: ``runs`` (every push), ``path_filtered`` (a ``paths`` or
``paths-ignore`` filter, so a push may start no run) or ``no_push``.

Anything the reader cannot read exactly refuses
``workflow_paths_unreadable: <path>``: YAML the loader refuses, an ``on`` or
trigger of another shape, a filter value that is not a non-empty list of
strings, an unknown ``pull_request`` type, both halves of a forbidden pair
(``branches`` with ``branches-ignore``, ``paths`` with ``paths-ignore``), and
a pattern outside the grammar read here. A refusal never drops a workflow.
"""
from __future__ import annotations

import fnmatch
import re

from .refusal import refuse

PULL_REQUEST_TYPES = frozenset((
    "assigned", "unassigned", "labeled", "unlabeled", "opened", "edited", "closed", "reopened",
    "synchronize", "converted_to_draft", "locked", "unlocked", "enqueued", "dequeued", "milestoned",
    "demilestoned", "ready_for_review", "review_requested", "review_request_removed",
    "auto_merge_enabled", "auto_merge_disabled",
))
PULL_REQUEST_DEFAULT_TYPES = ("opened", "synchronize", "reopened")
# The types that fire on a head before it merges, READY included.
PULL_REQUEST_PRE_MERGE_TYPES = frozenset(("opened", "synchronize", "reopened", "ready_for_review"))
BRANCH_FILTER_OUTSIDE = frozenset("?+[]!\\{}")
PATHS_IGNORE_OUTSIDE = frozenset("?[]{}")
RUNS = "runs"
PATH_FILTERED = "path_filtered"
NO_PUSH = "no_push"

_LOADER = None


def _unreadable(path: str, why: str) -> None:
    refuse("workflow_paths_unreadable", f"{path} ({why})")


def _strict_loader():
    """A PyYAML safe loader that refuses duplicate keys; ``pyyaml_missing`` when PyYAML is absent."""
    global _LOADER
    try:
        import yaml
    except ImportError:
        refuse("pyyaml_missing", "PyYAML is a declared dependency of review-gate; install it to read workflow files")
    if _LOADER is not None and _LOADER[0] is yaml:
        return yaml, _LOADER[1]

    class StrictUniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            try:
                hash(key)
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    None, None, "an unhashable key", key_node.start_mark) from None
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key: {key!r}", key_node.start_mark)
            mapping[key] = loader.construct_object(value_node, deep=True)
        return mapping

    StrictUniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping)
    _LOADER = (yaml, StrictUniqueKeyLoader)
    return _LOADER


def load_workflow(path: str, text: str):
    """The parsed workflow, or ``workflow_paths_unreadable`` (``pyyaml_missing`` with no PyYAML)."""
    yaml, loader = _strict_loader()
    try:
        return yaml.load(text, Loader=loader)
    except Exception as exc:  # any loader failure is an unreadable workflow
        lines = str(exc).splitlines()
        _unreadable(path, f"not YAML the reader parses: {lines[0] if lines else type(exc).__name__}")


# ---- patterns ------------------------------------------------------------

def pattern_match(pattern: str, path: str) -> bool:
    """Whether ``pattern`` covers ``path`` segment by segment; ``**`` is zero or more whole segments."""
    parts = path.split("/")
    reached = [True] + [False] * len(parts)
    for segment in pattern.split("/"):
        if segment == "**":
            seen = False
            spread = []
            for value in reached:
                seen = seen or value
                spread.append(seen)
            reached = spread
        else:
            reached = [False] + [
                reached[j] and fnmatch.fnmatchcase(parts[j], segment) for j in range(len(parts))
            ]
    return reached[len(parts)]


def ignore_match(pattern: str, path: str) -> bool:
    """pattern_match, with a trailing ``/**`` after a prefix matching one or more segments."""
    if pattern != "**" and pattern.endswith("/**"):
        pattern = pattern[: -len("/**")] + "/*/**"
    return pattern_match(pattern, path)


def path_filter_readable(pattern: str) -> bool:
    if not pattern or pattern.startswith("!") or "\\" in pattern or "+" in pattern:
        return False
    if "?" in pattern or "[" in pattern:
        return False
    for segment in pattern.split("/"):
        if segment in ("", ".", ".."):
            return False
        if "**" in segment and segment != "**":
            return False
    return True


def paths_ignore_readable(pattern: str) -> bool:
    return path_filter_readable(pattern) and not any(
        char in PATHS_IGNORE_OUTSIDE or char.isspace() for char in pattern
    )


def branch_filter_regex(pattern: str):
    """The regular expression of a GitHub branch filter pattern, or None when outside the grammar."""
    if (
        not pattern
        or "***" in pattern
        or any(char in BRANCH_FILTER_OUTSIDE or char.isspace() for char in pattern)
    ):
        return None
    segments = pattern.split("/")
    if "" in segments:
        return None
    parts = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**" and not last:
            parts.append("(?:.*/)?")
            continue
        parts.append(".*".join(
            "[^/]*".join(re.escape(literal) for literal in piece.split("*")) for piece in segment.split("**")
        ))
        if not last:
            parts.append("/")
    return re.compile("".join(parts))


# ---- triggers ------------------------------------------------------------

def _on(path: str, doc):
    if not isinstance(doc, dict):
        _unreadable(path, "the workflow is not a mapping")
    # YAML 1.1, which PyYAML reads, loads an unquoted `on` key as True.
    keys = [key for key in doc if key == "on" or key is True]
    if len(keys) != 1:
        _unreadable(path, "the workflow has no single `on` key")
    return doc[keys[0]]


def _filter_list(path: str, event: str, trigger: dict, key: str) -> list:
    values = trigger[key]
    if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
        _unreadable(path, f"`on.{event}.{key}` is not a non-empty list of strings")
    return values


def _branch_filter_admits(path: str, event: str, trigger: dict, key: str, branch: str) -> bool:
    matched = False
    for pattern in _filter_list(path, event, trigger, key):
        regex = branch_filter_regex(pattern)
        if regex is None:
            _unreadable(path, f"branch pattern {pattern!r} is outside the branch filter grammar")
        matched = matched or regex.fullmatch(branch) is not None
    return matched


def _event_trigger(path: str, on, event: str):
    """(named, trigger mapping or None) for one event of ``on``."""
    if isinstance(on, str):
        return on == event, None
    if isinstance(on, list):
        if not all(isinstance(item, str) for item in on):
            _unreadable(path, "`on` lists an event that is not a name")
        return event in on, None
    if not isinstance(on, dict):
        _unreadable(path, "`on` is not an event, a list or a mapping")
    if event not in on:
        return False, None
    trigger = on[event]
    if trigger is None:
        return True, None
    if not isinstance(trigger, dict):
        _unreadable(path, f"`on.{event}` is not a mapping")
    return True, trigger


def _pull_request_filter(path: str, doc, base: str) -> tuple:
    """(runs on a pull request into ``base``, its ``paths`` list or None, its ``paths-ignore`` list or None)."""
    named, trigger = _event_trigger(path, _on(path, doc), "pull_request")
    if not named:
        return False, None, None
    if trigger is None:
        return True, None, None
    if "paths" in trigger and "paths-ignore" in trigger:
        _unreadable(path, "`on.pull_request` has both `paths` and `paths-ignore`")
    if "branches" in trigger and "branches-ignore" in trigger:
        _unreadable(path, "`on.pull_request` has both `branches` and `branches-ignore`")
    runs = True
    types = PULL_REQUEST_DEFAULT_TYPES
    if "types" in trigger:
        types = _filter_list(path, "pull_request", trigger, "types")
        for kind in types:
            if kind not in PULL_REQUEST_TYPES:
                _unreadable(path, f"`on.pull_request.types` names an unknown type {kind!r}")
    if not PULL_REQUEST_PRE_MERGE_TYPES.intersection(types):
        runs = False
    if "branches" in trigger and not _branch_filter_admits(path, "pull_request", trigger, "branches", base):
        runs = False
    if "branches-ignore" in trigger and _branch_filter_admits(path, "pull_request", trigger, "branches-ignore", base):
        runs = False
    if "paths-ignore" in trigger:
        ignore = _filter_list(path, "pull_request", trigger, "paths-ignore")
        for pattern in ignore:
            if not paths_ignore_readable(pattern):
                _unreadable(path, f"paths-ignore pattern {pattern!r} is outside the paths-ignore grammar")
        return runs, None, ignore
    if "paths" not in trigger:
        return runs, None, None
    patterns = _filter_list(path, "pull_request", trigger, "paths")
    for pattern in patterns:
        if not path_filter_readable(pattern):
            _unreadable(path, f"path pattern {pattern!r} is outside the shared glob grammar")
    return runs, patterns, None


def workflow_name(path: str, doc) -> str:
    """A workflow's ``name``, or its path when it has none, as GitHub names its runs."""
    title = doc.get("name") if isinstance(doc, dict) else None
    return title if isinstance(title, str) and title else path


def selected_workflows(docs: dict, paths, base: str) -> dict:
    """``{path: name}`` of the workflows in ``docs`` a pull request into ``base`` changing ``paths`` runs."""
    if not isinstance(base, str) or not base:
        refuse("workflow_base_unresolved", f"the pull request's base branch is {base!r}")
    selected = {}
    for path in sorted(docs):
        doc = docs[path]
        runs, patterns, ignore = _pull_request_filter(path, doc, base)
        if not runs:
            continue
        if patterns is not None and not any(
            pattern_match(pattern, changed) for pattern in patterns for changed in paths
        ):
            continue
        if ignore is not None and paths and all(
            any(ignore_match(pattern, changed) for pattern in ignore) for changed in paths
        ):
            continue
        selected[path] = workflow_name(path, doc)
    return selected


def push_trigger(path: str, doc, branch: str) -> str:
    """How the workflow runs on a push to ``branch``: RUNS, PATH_FILTERED or NO_PUSH."""
    named, trigger = _event_trigger(path, _on(path, doc), "push")
    if not named:
        return NO_PUSH
    if trigger is None:
        return RUNS
    for key in ("branches", "branches-ignore", "tags", "tags-ignore", "paths", "paths-ignore"):
        if key in trigger:
            _filter_list(path, "push", trigger, key)
    if "paths" in trigger and "paths-ignore" in trigger:
        _unreadable(path, "`on.push` has both `paths` and `paths-ignore`")
    if "branches" in trigger and "branches-ignore" in trigger:
        _unreadable(path, "`on.push` has both `branches` and `branches-ignore`")
    has_branch_filter = "branches" in trigger or "branches-ignore" in trigger
    has_tag_filter = "tags" in trigger or "tags-ignore" in trigger
    if has_tag_filter and not has_branch_filter:
        # GitHub runs a push workflow with only tag filters on tag pushes only.
        return NO_PUSH
    if "branches" in trigger and not _branch_filter_admits(path, "push", trigger, "branches", branch):
        return NO_PUSH
    if "branches-ignore" in trigger and _branch_filter_admits(path, "push", trigger, "branches-ignore", branch):
        return NO_PUSH
    if "paths" in trigger or "paths-ignore" in trigger:
        return PATH_FILTERED
    return RUNS


def push_path_filter(path: str, doc) -> tuple:
    """The tip's validated push ``(paths, paths-ignore)`` lists, absent lists as None."""
    named, trigger = _event_trigger(path, _on(path, doc), "push")
    if not named or trigger is None:
        return None, None
    if "paths" in trigger and "paths-ignore" in trigger:
        _unreadable(path, "`on.push` has both `paths` and `paths-ignore`")
    if "paths-ignore" in trigger:
        ignore = _filter_list(path, "push", trigger, "paths-ignore")
        for pattern in ignore:
            if not paths_ignore_readable(pattern):
                _unreadable(path, f"paths-ignore pattern {pattern!r} is outside the paths-ignore grammar")
        return None, ignore
    if "paths" not in trigger:
        return None, None
    patterns = _filter_list(path, "push", trigger, "paths")
    for pattern in patterns:
        if not path_filter_readable(pattern):
            _unreadable(path, f"path pattern {pattern!r} is outside the shared glob grammar")
    return patterns, None
