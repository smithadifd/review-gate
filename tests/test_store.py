"""Stage 1: the ledger database the records port names must not sit where a lane can write."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from review_gate.records import DatabasePaths
from review_gate.refusal import Refusal
from review_gate.store import DATA_DIR, StoreCheck, root_lines

from rig import make_store


def refused(store, paths=None) -> Refusal:
    with pytest.raises(Refusal) as caught:
        store.check.check(paths or store.paths)
    return caught.value


def settings(directory: Path, roots, name="lane.settings.json") -> Path:
    path = directory / name
    path.write_text(json.dumps({"sandbox": {"filesystem": {"allowWrite": [str(r) for r in roots]}}}), encoding="utf-8")
    return path


def test_a_clean_store_passes(tmp_path):
    store = make_store(tmp_path)
    assert store.check.check(store.paths) == str(store.root)


def test_forged_matrix_row_10_database_in_a_lane_writable_root_refuses_the_merge(rig):
    """§ 10 row 10: the database directory is a lane's write root; the merge refuses at stage 1."""
    settings(Path(os.path.realpath(rig.tmp_path)), [rig.store.root])
    result = rig.run()
    assert result.code == 2
    assert result.reason == "receipt_store_lane_writable", result.err
    assert rig.fake.calls() == []
    assert rig.port.names() == ["database_paths"]


def test_a_lane_write_root_inside_the_store_refuses(tmp_path):
    store = make_store(tmp_path)
    settings(Path(os.path.realpath(tmp_path)), [store.root / "inner"])
    assert refused(store).name == "receipt_store_lane_writable"


def test_a_lane_write_root_above_the_store_refuses(tmp_path):
    store = make_store(tmp_path)
    settings(Path(os.path.realpath(tmp_path)), [Path(os.path.realpath(tmp_path))])
    assert refused(store).name == "receipt_store_lane_writable"


def test_an_unrelated_lane_write_root_passes(tmp_path):
    store = make_store(tmp_path)
    elsewhere = Path(os.path.realpath(tmp_path)) / "elsewhere"
    elsewhere.mkdir()
    settings(Path(os.path.realpath(tmp_path)), [elsewhere])
    assert store.check.check(store.paths) == str(store.root)


def test_a_symlink_spelling_of_the_store_is_the_store(tmp_path):
    store = make_store(tmp_path)
    alias = Path(os.path.realpath(tmp_path)) / "alias"
    alias.symlink_to(store.root)
    settings(Path(os.path.realpath(tmp_path)), [alias])
    assert refused(store).name == "receipt_store_lane_writable"


def test_a_case_spelling_of_the_store_is_compared_by_identity(tmp_path):
    """On a case-insensitive filesystem (macOS) the upper-case spelling is the store and refuses.

    On a case-sensitive filesystem the spelling names no directory, so nothing
    overlaps; this test then shows only that the comparison is by identity.
    Only a macOS run proves the alias case.
    """
    store = make_store(tmp_path)
    upper = Path(str(store.root).upper())
    settings(Path(os.path.realpath(tmp_path)), [upper])
    if upper.exists() and os.path.samefile(upper, store.root):
        assert refused(store).name == "receipt_store_lane_writable"
    else:
        assert store.check.check(store.paths) == str(store.root)


def test_a_registered_lane_root_refuses(tmp_path):
    store = make_store(tmp_path)
    registry = Path(os.path.realpath(tmp_path)) / "lane-roots.tsv"
    registry.write_text(f"path\trepo\tbranch\tcreated_at\n{store.root}\tr\tb\t2026-11-21\n", encoding="utf-8")
    assert refused(store).name == "receipt_store_in_lane_root"


def test_a_malformed_registry_refuses(tmp_path):
    store = make_store(tmp_path)
    registry = Path(os.path.realpath(tmp_path)) / "lane-roots.tsv"
    registry.write_text("not a header\n", encoding="utf-8")
    assert refused(store).name == "receipt_store_in_lane_root"


def test_an_unreadable_settings_file_refuses(tmp_path):
    store = make_store(tmp_path)
    (Path(os.path.realpath(tmp_path)) / "lane.settings.json").write_text("{", encoding="utf-8")
    assert refused(store).name == "receipt_store_lane_writable"


def test_an_unopenable_lane_registry_refuses(tmp_path):
    store = make_store(tmp_path)
    (Path(os.path.realpath(tmp_path)) / "lane-roots.tsv").mkdir()
    assert refused(store).name == "receipt_store_in_lane_root"


def test_an_unopenable_settings_file_refuses_by_the_store_reason(tmp_path):
    store = make_store(tmp_path)
    (Path(os.path.realpath(tmp_path)) / "lane.settings.json").mkdir()
    assert refused(store).name == "receipt_store_lane_writable"


def test_a_store_under_a_handback_directory_refuses(tmp_path):
    base = Path(os.path.realpath(tmp_path)) / "handback-row"
    base.mkdir()
    store = make_store(base)
    assert refused(store).name == "receipt_store_in_handback"


def test_a_store_inside_a_git_tree_refuses(tmp_path):
    base = Path(os.path.realpath(tmp_path)) / "checkout"
    (base / ".git").mkdir(parents=True)
    store = make_store(base)
    assert refused(store).name == "receipt_store_in_lane_root"


def test_a_store_holding_a_git_directory_refuses(tmp_path):
    store = make_store(tmp_path)
    (store.root / "nested" / ".git").mkdir(parents=True)
    assert refused(store).name == "receipt_store_in_lane_root"


def test_a_store_under_omp_refuses(tmp_path):
    base = Path(os.path.realpath(tmp_path)) / ".omp"
    base.mkdir()
    store = make_store(base)
    assert refused(store).name == "receipt_store_in_lane_root"


def test_a_store_in_a_temp_root_refuses(tmp_path):
    store = make_store(tmp_path)
    inner = store.lane_tmp / "ledger"
    inner.mkdir(mode=0o700)
    database = inner / "ledger.db"
    database.write_bytes(b"")
    database.chmod(0o600)
    paths = DatabasePaths(str(database), str(database) + ".documents")
    assert refused(store, paths).name == "receipt_store_in_temp_root"


def test_a_store_in_tmpdir_refuses(tmp_path):
    store = make_store(tmp_path)
    check = StoreCheck(
        str(store.temp_roots), str(store.sandbox_roots),
        environ={"TMPDIR": str(store.root), "HOME": str(store.user_home)},
        gettempdir=lambda: str(store.lane_tmp),
    )
    with pytest.raises(Refusal) as caught:
        check.check(store.paths)
    assert caught.value.name == "receipt_store_in_temp_root"


def test_a_store_in_a_sandbox_write_root_refuses(tmp_path):
    store = make_store(tmp_path)
    inner = store.user_home / ".npm" / "_logs" / "ledger"
    inner.mkdir(parents=True, mode=0o700)
    database = inner / "ledger.db"
    database.write_bytes(b"")
    database.chmod(0o600)
    paths = DatabasePaths(str(database), str(database) + ".documents")
    assert refused(store, paths).name == "receipt_store_in_sandbox_root"


def test_a_symlinked_store_refuses(tmp_path):
    store = make_store(tmp_path)
    link = Path(os.path.realpath(tmp_path)) / "linked"
    link.symlink_to(store.root)
    paths = DatabasePaths(str(link / "ledger.db"), str(link / "ledger.db.documents"))
    assert refused(store, paths).name == "receipt_store_symlink"


def test_a_group_writable_store_refuses(tmp_path):
    store = make_store(tmp_path)
    store.root.chmod(0o770)
    assert refused(store).name == "receipt_store_permissions"


def test_a_hardlinked_database_refuses(tmp_path):
    store = make_store(tmp_path)
    os.link(store.paths.database, Path(os.path.realpath(tmp_path)) / "second-name.db")
    assert refused(store).name == "db_hardlinked"


def test_a_missing_database_refuses(tmp_path):
    store = make_store(tmp_path)
    os.unlink(store.paths.database)
    assert refused(store).name == "receipt_store_database_missing"


def test_a_symlinked_database_refuses(tmp_path):
    store = make_store(tmp_path)
    real = Path(os.path.realpath(tmp_path)) / "real.db"
    real.write_bytes(b"")
    os.unlink(store.paths.database)
    os.symlink(real, store.paths.database)
    assert refused(store).name == "receipt_store_symlink"


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_a_symlinked_or_hardlinked_journal_file_refuses(tmp_path, suffix):
    store = make_store(tmp_path)
    journal = Path(store.paths.database + suffix)
    target = Path(os.path.realpath(tmp_path)) / "elsewhere"
    target.write_bytes(b"")
    journal.symlink_to(target)
    assert refused(store).name == "receipt_store_symlink"
    journal.unlink()
    journal.write_bytes(b"")
    journal.chmod(0o600)
    os.link(journal, Path(os.path.realpath(tmp_path)) / f"second{suffix}")
    assert refused(store).name == "db_hardlinked"


def test_a_documents_folder_that_is_not_a_directory_refuses(tmp_path):
    store = make_store(tmp_path)
    Path(store.paths.documents).write_bytes(b"")
    assert refused(store).name == "receipt_store_documents_invalid"


def test_a_documents_folder_away_from_the_database_refuses(tmp_path):
    store = make_store(tmp_path)
    paths = DatabasePaths(store.paths.database, str(Path(os.path.realpath(tmp_path)) / "docs"))
    assert refused(store, paths).name == "receipt_store_documents_invalid"


def test_a_relative_database_path_refuses(tmp_path):
    store = make_store(tmp_path)
    assert refused(store, DatabasePaths("ledger.db", "ledger.db.documents")).name == "receipt_store_database_invalid"


@pytest.mark.parametrize("shape", ["missing", "empty", "symlink", "relative", "directory"])
@pytest.mark.parametrize("which", ["temp", "sandbox"])
def test_a_bad_root_list_refuses_every_store(tmp_path, shape, which):
    store = make_store(tmp_path)
    data = store.temp_roots if which == "temp" else store.sandbox_roots
    reason = "receipt_temp_roots_unreadable" if which == "temp" else "receipt_sandbox_roots_unreadable"
    data.unlink()
    if shape == "empty":
        data.write_text("# only a comment\n\n", encoding="utf-8")
    elif shape == "symlink":
        real = data.parent / "real.txt"
        real.write_text("/tmp\n" if which == "temp" else ".npm/_logs\n", encoding="utf-8")
        data.symlink_to(real)
    elif shape == "relative":
        data.write_text("tmp\n" if which == "temp" else "/abs/path\n", encoding="utf-8")
    elif shape == "directory":
        data.mkdir()
    assert refused(store).name == reason


def test_the_shipped_root_lists_name_roots():
    temp = root_lines(str(DATA_DIR / "temp-roots.txt"), "receipt_temp_roots_unreadable", absolute=True)
    assert "/tmp" in temp and "/var/folders" in temp
    sandbox = root_lines(str(DATA_DIR / "sandbox-roots.txt"), "receipt_sandbox_roots_unreadable", absolute=False)
    assert ".claude/debug" in sandbox


def test_the_shipped_check_refuses_a_database_under_the_temp_directory(tmp_path):
    """The package-data lists are what the shipped check reads; pytest's tmp_path is under a listed temp root."""
    store = make_store(tmp_path)
    with pytest.raises(Refusal) as caught:
        StoreCheck.shipped().check(store.paths)
    assert caught.value.name == "receipt_store_in_temp_root"


def test_a_home_that_is_the_filesystem_root_refuses(tmp_path):
    store = make_store(tmp_path)
    check = StoreCheck(
        str(store.temp_roots), str(store.sandbox_roots),
        environ={"TMPDIR": str(store.lane_tmp), "HOME": "/"},
        gettempdir=lambda: str(store.lane_tmp),
    )
    with pytest.raises(Refusal) as caught:
        check.check(store.paths)
    assert caught.value.name == "receipt_sandbox_roots_unreadable"
