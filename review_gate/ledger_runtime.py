"""Load only the ledger-hooks plugin, never a checkout-local ledger package.

PYTHONPATH locates the dependency, not policy. The candidates are the
absolute entries of PYTHONPATH, as the check's environment names it, and
each entry's ``plugins/ledger-hooks``; a relative entry is skipped, never
resolved against the working directory. The current directory and
``sys.path[0]`` (under ``python3 -m`` that is the current directory) are
never a plugin root: an entry or root that is either one, by file identity,
is skipped.

The first candidate holding the plugin manifest is the plugin root. It must
pass the directory checks stage 1 applies to the database's directory
(``StoreCheck.check_root``): no git tree, hand-back, lane root or
lane-writable root, no temp root and no sandbox write root, owned by the
caller and not group or other writable. A refusal keeps the store check's
name and names the plugin root. Then the manifest and the package must
provide exactly v0.6.1. Nothing from a candidate root is read, imported or
executed before it passes.

The check is injectable as stage 1's is: the command uses
``StoreCheck.shipped()`` (the process environment and the shipped root
lists); a test passes a ``StoreCheck`` built from Python with its own roots.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

from .refusal import Refusal, refuse
from .store import StoreCheck

VERSION = "0.6.1"


def _identity(path):
    """(st_dev, st_ino) of ``path``, or None when it cannot be read."""
    try:
        status = os.stat(path)
    except (OSError, ValueError):
        return None
    return status.st_dev, status.st_ino


def _never_roots():
    """The identities of the current directory and ``sys.path[0]``: never a plugin root."""
    found = {_identity(".")}
    if sys.path:
        found.add(_identity(sys.path[0] or "."))
    found.discard(None)
    return found


def _candidates(check):
    """(root, manifest) for each candidate holding a plugin manifest, in PYTHONPATH order."""
    never = _never_roots()
    roots = []
    for entry in check.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        base = Path(entry)
        if _identity(base) in never:
            continue
        for root in (base, base / "plugins" / "ledger-hooks"):
            if _identity(root) in never:
                continue
            manifest = root / ".claude-plugin" / "plugin.json"
            if manifest.is_file():
                roots.append((root, manifest))
    return roots


def load_ledger(check=None):
    check = StoreCheck.shipped() if check is None else check
    roots = _candidates(check)
    if not roots:
        refuse("ledger_plugin_missing", "put the ledger-hooks plugin root on PYTHONPATH")
    root, manifest = roots[0]
    try:
        root = Path(check.check_root(str(root)))
    except Refusal as exc:
        refuse(exc.name, f"ledger-hooks plugin root {exc.detail}")
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        refuse("ledger_plugin_invalid", "the ledger-hooks manifest is unreadable")
    if metadata.get("name") != "ledger-hooks" or metadata.get("version") != VERSION:
        refuse("ledger_version_unsupported", "ledger-hooks v0.6.1 is required")
    package = root / "ledger"
    if package.is_symlink() or package.resolve() != package or not (package / "__init__.py").is_file():
        refuse("ledger_plugin_invalid", "the plugin ledger package must be local to its root")
    existing = sys.modules.get("ledger")
    if existing is not None:
        if Path(existing.__file__).resolve() != package / "__init__.py":
            refuse("ledger_origin_invalid", "a different ledger package is already imported")
        module = existing
    else:
        spec = importlib.util.spec_from_file_location(
            "ledger", package / "__init__.py", submodule_search_locations=[str(package)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["ledger"] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules["ledger"]
            raise
    if module.__version__ != VERSION:
        refuse("ledger_version_unsupported", "ledger v0.6.1 is required")
    return module


def ledger_module(name):
    return importlib.import_module("ledger." + name)
