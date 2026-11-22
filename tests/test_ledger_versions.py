"""The ledger-hooks versions the loader accepts: an exact set, and the package must match the manifest.

The accepted versions are listed here on purpose, not imported from production:
a change to the set has to change this file too.
"""
import json
from pathlib import Path
import sys
import uuid

import pytest

from ledger_rig import plugin_check_for
from review_gate.ledger_runtime import load_ledger
from review_gate.refusal import Refusal

ACCEPTED = ["0.6.1", "0.6.2", "0.6.3", "0.7.0"]
NEIGHBOURS = ["0.6.0", "0.7.1", "0.7.1-dev", "0.7", "v0.7.0", "0.7.0 ", "1.0.0",
              # inside the range from the lowest to the highest accepted version, but not in the set
              "0.6.4", "0.6.10",
              # loose spellings of an accepted version
              " 0.7.0", "0.7.0\n", "0.7.00", "0.6.1.0", "", "\uff10.\uff17.\uff10"]


def build_root(plugin, manifest_version, package_version, marker, name="ledger-hooks"):
    """A plugin root beside the session's own (so it passes the directory checks) whose package records that it ran."""
    root = plugin.parent / f"versions-{uuid.uuid4().hex}"
    (root / ".claude-plugin").mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": manifest_version}))
    (root / "ledger").mkdir()
    (root / "ledger" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('executed')\n__version__ = {package_version!r}\n")
    return root


def refusal(check):
    with pytest.raises(Refusal) as exc:
        load_ledger(check)
    return exc.value


@pytest.mark.parametrize("version", ACCEPTED)
def test_each_accepted_version_loads(ledger_rig, tmp_path, monkeypatch, version):
    marker = tmp_path / "ran"
    root = build_root(ledger_rig.plugin, version, version, marker)
    monkeypatch.delitem(sys.modules, "ledger")
    module = load_ledger(plugin_check_for(ledger_rig.plugin_check, root))
    assert module.__version__ == version
    assert Path(module.__file__) == root / "ledger" / "__init__.py"
    assert marker.exists()


@pytest.mark.parametrize("version", NEIGHBOURS)
def test_a_neighbouring_version_refuses_before_anything_runs(ledger_rig, tmp_path, monkeypatch, version):
    marker = tmp_path / "ran"
    root = build_root(ledger_rig.plugin, version, version, marker)
    monkeypatch.delitem(sys.modules, "ledger")
    found = refusal(plugin_check_for(ledger_rig.plugin_check, root))
    assert found.name == "ledger_version_unsupported", found.line()
    for accepted in ACCEPTED:
        assert accepted in found.detail, found.line()
    assert not marker.exists(), "a package from a refused root ran"
    assert "ledger" not in sys.modules, "a package from a refused root was imported"


@pytest.mark.parametrize("manifest_version, package_version", [("0.6.3", "0.7.0"), ("0.7.0", "0.6.3")])
def test_a_package_that_differs_from_its_manifest_refuses(ledger_rig, tmp_path, monkeypatch,
                                                          manifest_version, package_version):
    root = build_root(ledger_rig.plugin, manifest_version, package_version, tmp_path / "ran")
    monkeypatch.delitem(sys.modules, "ledger")
    found = refusal(plugin_check_for(ledger_rig.plugin_check, root))
    assert found.name == "ledger_version_unsupported", found.line()
    assert manifest_version in found.detail, found.line()


def test_a_second_call_on_a_refused_root_refuses_again(ledger_rig, tmp_path, monkeypatch):
    # After the first refusal the package is still imported, so the second call takes the
    # already-imported path; it must hold that module to the manifest's version as well.
    root = build_root(ledger_rig.plugin, "0.6.3", "0.7.0", tmp_path / "ran")
    check = plugin_check_for(ledger_rig.plugin_check, root)
    monkeypatch.delitem(sys.modules, "ledger")
    assert refusal(check).name == "ledger_version_unsupported"
    assert "ledger" in sys.modules
    assert refusal(check).name == "ledger_version_unsupported"


def test_a_second_call_on_an_accepted_root_returns_the_same_module(ledger_rig, tmp_path, monkeypatch):
    root = build_root(ledger_rig.plugin, "0.7.0", "0.7.0", tmp_path / "ran")
    check = plugin_check_for(ledger_rig.plugin_check, root)
    monkeypatch.delitem(sys.modules, "ledger")
    assert load_ledger(check) is load_ledger(check)


@pytest.mark.parametrize("name", ["not-ledger-hooks", "", None])
def test_a_plugin_with_another_name_refuses_before_anything_runs(ledger_rig, tmp_path, monkeypatch, name):
    marker = tmp_path / "ran"
    root = build_root(ledger_rig.plugin, "0.7.0", "0.7.0", marker, name=name)
    monkeypatch.delitem(sys.modules, "ledger")
    assert refusal(plugin_check_for(ledger_rig.plugin_check, root)).name == "ledger_version_unsupported"
    assert not marker.exists(), "a package from a refused root ran"
    assert "ledger" not in sys.modules


@pytest.mark.parametrize("manifest_version", [None, 7, ["0.7.0"]])
def test_a_manifest_version_that_is_not_a_string_refuses(ledger_rig, tmp_path, monkeypatch, manifest_version):
    marker = tmp_path / "ran"
    root = build_root(ledger_rig.plugin, manifest_version, "0.7.0", marker)
    monkeypatch.delitem(sys.modules, "ledger")
    assert refusal(plugin_check_for(ledger_rig.plugin_check, root)).name == "ledger_version_unsupported"
    assert not marker.exists()
