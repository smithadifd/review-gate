"""The ledger-hooks plugin root: an operator's PYTHONPATH entry that passes stage 1's directory checks.

The current directory and sys.path[0] are never a plugin root, and nothing in
a candidate root runs before it passes.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ledger_rig import installed_version, plugin_check_for
from rig import Repo, make_store, write_roots
from test_ledger_records import refused
from review_gate.ledger_runtime import load_ledger
from review_gate.refusal import Refusal
from review_gate.store import StoreCheck

ROOT = Path(__file__).resolve().parents[1]


def plant(directory, marker):
    """A directory that looks like a ledger-hooks root whose ledger package records that it ran."""
    (directory / ".claude-plugin").mkdir(parents=True)
    (directory / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ledger-hooks", "version": "0.6.1"}))
    (directory / "ledger").mkdir()
    (directory / "ledger" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('executed')\nraise RuntimeError('planted ledger ran')\n")
    return directory


def copy_plugin(directory):
    """A ledger-hooks root holding the installed ledger, built without the session plugin fixture."""
    source = Path(importlib.util.find_spec("ledger").origin).parent
    shutil.copytree(source, directory / "ledger", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (directory / ".claude-plugin").mkdir()
    (directory / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "ledger-hooks", "version": installed_version(source)}))
    return directory


def refused_root(name, check, plugin):
    with pytest.raises(Refusal) as exc:
        load_ledger(check)
    assert exc.value.name == name, exc.value.line()
    assert exc.value.detail.startswith(f"ledger-hooks plugin root {plugin} "), exc.value.line()


def test_planted_plugin_in_working_directory_never_executes(tmp_path):
    # The review's probe: python3 -m review_gate puts the working directory at sys.path[0],
    # with the real plugin on PYTHONPATH. No session fixture: this runs the shipped command only.
    plugin = copy_plugin(tmp_path / "plugin")
    marker = tmp_path / "planted-ran"
    probe = plant(tmp_path / "probe", marker)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(plugin)]),
               LEDGER_DB=str(tmp_path / "store" / "ledger.db"), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-m", "review_gate", *Repo().argv()],
                          cwd=probe, env=env, capture_output=True, text=True)
    assert not marker.exists(), "a plugin planted in the working directory executed"
    assert done.returncode == 2, done.stderr
    # The real plugin root sits under the platform temp root here, so it refuses by name first.
    assert done.stderr.startswith(f"receipt_store_in_temp_root: ledger-hooks plugin root {plugin} "), done.stderr


@pytest.mark.parametrize("layout", ["root", "plugins", "nested"])
def test_working_directory_is_never_a_plugin_root(ledger_rig, tmp_path, monkeypatch, layout):
    # root: the entry is the working directory; plugins: its plugins/ledger-hooks holds the plant;
    # nested: the working directory is the plugins/ledger-hooks of an entry that is not itself it.
    r = ledger_rig
    marker = tmp_path / "planted-ran"
    entry = tmp_path / "planted"
    planted = plant(entry if layout == "root" else entry / "plugins" / "ledger-hooks", marker)
    monkeypatch.chdir(planted if layout == "nested" else entry)
    monkeypatch.delitem(sys.modules, "ledger")
    module = load_ledger(plugin_check_for(r.plugin_check, entry, r.plugin))
    assert not marker.exists(), "a plugin root in the working directory executed"
    assert Path(module.__file__) == r.plugin / "ledger" / "__init__.py"


def test_sys_path_zero_is_never_a_plugin_root(ledger_rig, tmp_path, monkeypatch):
    r = ledger_rig
    marker = tmp_path / "planted-ran"
    entry = plant(tmp_path / "script-directory", marker)
    monkeypatch.setattr(sys, "path", [str(entry), *sys.path])
    monkeypatch.delitem(sys.modules, "ledger")
    module = load_ledger(plugin_check_for(r.plugin_check, entry, r.plugin))
    assert not marker.exists(), "a plugin root at sys.path[0] executed"
    assert Path(module.__file__) == r.plugin / "ledger" / "__init__.py"


def test_a_relative_pythonpath_entry_is_never_resolved(ledger_rig, monkeypatch):
    # A relative entry would name a directory under the working directory; it is skipped.
    r = ledger_rig
    monkeypatch.chdir(r.plugin.parent)
    refused("ledger_plugin_missing", load_ledger, plugin_check_for(r.plugin_check, r.plugin.name))


def test_a_plugin_only_on_sys_path_is_not_a_candidate(tmp_path, monkeypatch):
    # PYTHONPATH locates the plugin; an entry that is only on sys.path is never a root.
    # No session fixture: it would itself load through the scan this test guards.
    plugin = copy_plugin(tmp_path / "plugin")
    monkeypatch.setattr(sys, "path", [*sys.path[:1], str(plugin), *sys.path[1:]])
    monkeypatch.delitem(sys.modules, "ledger", raising=False)
    refused("ledger_plugin_missing", load_ledger, plugin_check_for(make_store(tmp_path).check))


@pytest.mark.parametrize("where", ["listed", "TMPDIR", "gettempdir"])
def test_plugin_root_under_a_temp_root_refuses(ledger_rig, tmp_path, where):
    r = ledger_rig
    base = r.plugin_check
    temp_roots = base.temp_roots_file
    if where == "listed":
        temp_roots = str(write_roots(tmp_path / "roots" / "temp-roots.txt", [str(r.plugin.parent)]))
    environ = dict(base.environ, TMPDIR=str(r.plugin.parent)) if where == "TMPDIR" else base.environ
    gettempdir = (lambda: str(r.plugin.parent)) if where == "gettempdir" else base.gettempdir
    check = StoreCheck(temp_roots, base.sandbox_roots_file, environ=environ, gettempdir=gettempdir)
    refused_root("receipt_store_in_temp_root", check, r.plugin)


def test_plugin_root_under_a_sandbox_write_root_refuses(ledger_rig, tmp_path):
    r = ledger_rig
    base = r.plugin_check
    sandbox_roots = write_roots(tmp_path / "roots" / "sandbox-roots.txt", [r.plugin.name])
    check = StoreCheck(base.temp_roots_file, str(sandbox_roots),
                       environ=dict(base.environ, HOME=str(r.plugin.parent)), gettempdir=base.gettempdir)
    refused_root("receipt_store_in_sandbox_root", check, r.plugin)
