"""Load only the ledger-hooks plugin, never a checkout-local ledger package.

PYTHONPATH locates the dependency, not policy. Candidates must contain the
plugin manifest, live outside git/lane roots, and provide exactly v0.6.1.
No ledger code executes until its origin has passed the store's root checks.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

from .refusal import refuse
from .store import check_store

VERSION = "0.6.1"


def load_ledger():
    roots = []
    for entry in sys.path:
        if not entry:
            continue
        base = Path(entry)
        if not base.is_absolute():
            continue
        for root in (base, base / "plugins" / "ledger-hooks"):
            manifest = root / ".claude-plugin" / "plugin.json"
            if manifest.is_file():
                roots.append((root, manifest))
    if not roots:
        refuse("ledger_plugin_missing", "put the ledger-hooks plugin root on PYTHONPATH")
    root, manifest = roots[0]
    root = Path(check_store(str(root)))
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
