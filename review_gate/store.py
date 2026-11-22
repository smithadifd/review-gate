"""Stage 1: the ledger database must not sit where a lane can write.

The records port names the database file and its documents folder
(``DatabasePaths``); this stage checks exactly those paths, never a path it
resolves itself. The store is the directory that holds the database file.
The checks are the review runner's receipt-store checks, carried whole, with
the same refusal names:

- the store, or a directory above it, is a hand-back directory
  (``receipt_store_in_handback``), ``.omp``, inside a git work tree or a git
  directory, or under a root a ``lane-roots.tsv`` registry beside it lists
  (``receipt_store_in_lane_root``), or under a root a ``*.settings.json``
  lane settings file beside it lets a lane write
  (``receipt_store_lane_writable``); a lane root or a write root that lies
  inside the store refuses the same way;
- a directory below the store is a hand-back directory, ``.omp``, ``.git``,
  a git work tree or a git directory;
- the store overlaps a temp root: a root of the shipped temp-roots list,
  ``$TMPDIR`` or ``tempfile.gettempdir()`` (``receipt_store_in_temp_root``);
- the store overlaps a write root the Claude sandbox grants every lane, the
  shipped sandbox-roots list joined onto the caller's home directory
  (``receipt_store_in_sandbox_root``);
- the store is a symlink (``receipt_store_symlink``), missing
  (``receipt_store_missing``), not the caller's or group or other writable
  (``receipt_store_permissions``), or cannot be resolved
  (``receipt_store_unresolvable``).

Every overlap is compared by file identity (device and inode), never by
path text, so a case, Unicode or symlink spelling of one directory is that
directory. A root that does not exist yet is placed by its nearest existing
ancestor.

The root lists ship as package data (``data/temp-roots.txt`` and
``data/sandbox-roots.txt``). A missing, symlinked, unreadable or malformed
list, or one naming no root, refuses every store
(``receipt_temp_roots_unreadable``, ``receipt_sandbox_roots_unreadable``).

Then the database itself: the file must exist (``receipt_store_database_missing``)
as a regular file that is not a symlink, owned by the caller and not group
or other writable, with exactly one link (``db_hardlinked``: a lane holding a
second name could rewrite it without touching the store). Its ``-wal`` and
``-shm`` files are held to the same rules when they exist. The documents
folder must sit beside the file, and when it exists it must be a directory
that is not a symlink, owned by the caller and not group or other writable.

Any filesystem or parse error that escapes these checks refuses
``receipt_reader_failed``; nothing ends in a traceback.
"""
from __future__ import annotations

import errno
import json
import os
import pwd
import stat
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from .records import DatabasePaths
from .refusal import Refusal, refuse

LANE_ROOTS = "lane-roots.tsv"
# The lane-root registry's header line.
LANE_ROOTS_HEADER = "path\trepo\tbranch\tcreated_at"
LANE_SETTINGS = ".settings.json"
DATA_DIR = Path(__file__).resolve().parent / "data"
TEMP_ROOTS_FILE = "temp-roots.txt"
SANDBOX_ROOTS_FILE = "sandbox-roots.txt"
# A path that is not there; any other failure to read it refuses.
ABSENT = (errno.ENOENT, errno.ENOTDIR)


def _read_bytes(path: str, unreadable: str) -> bytes:
    """The bytes of a regular file, opened without following a final symlink or blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        refuse(unreadable, path)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            refuse(unreadable, f"{path} (not a regular file)")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        refuse(unreadable, path)
    finally:
        os.close(fd)
    return b"".join(chunks)


def lane_write_roots(settings: str, store: str) -> list:
    """The write roots a lane settings file grants, as written; refuse what cannot be read."""
    unreadable = f"{store} ({settings} unreadable)"
    try:
        doc = json.loads(_read_bytes(settings, "lane_settings_unreadable").decode("utf-8"))
        roots = doc["sandbox"]["filesystem"].get("allowWrite", [])
    except (Refusal, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        refuse("receipt_store_lane_writable", unreadable)
    if not isinstance(roots, list) or not all(isinstance(root, str) and root.startswith("/") for root in roots):
        refuse("receipt_store_lane_writable", f"{store} ({settings} names a write root that is not absolute)")
    return ["/" + root.lstrip("/") for root in roots]


def _unresolvable(store: str, path: str, error) -> None:
    refuse("receipt_store_unresolvable", f"{store} ({path}: {getattr(error, 'strerror', None) or error})")


def _probe(path: str, store: str):
    """``os.lstat(path)``, or None when nothing is there; any other failure refuses."""
    try:
        return os.lstat(path)
    except OSError as exc:
        if exc.errno in ABSENT:
            return None
        _unresolvable(store, path, exc)


def _identity(path: str, store: str):
    """(st_dev, st_ino) of ``path``, symlinks followed, or None when it does not exist."""
    try:
        status = os.stat(path)
    except OSError as error:
        if error.errno in ABSENT:
            return None
        _unresolvable(store, path, error)
    return status.st_dev, status.st_ino


def _lineage(path: str):
    """``path``, then each lexical ancestor of it, ending at the filesystem root."""
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _chain(path: str, store: str) -> list:
    """The identities of ``path`` and of each of its ancestors that exists."""
    found = (_identity(each, store) for each in _lineage(path))
    return [ident for ident in found if ident is not None]


def _store_identity(real: str, store: str) -> tuple:
    """The store's identity and the identity of every ancestor of it, nearest first."""
    chain = []
    for each in _lineage(real):
        ident = _identity(each, store)
        if ident is None:
            _unresolvable(store, each, "it no longer exists")
        chain.append(ident)
    return chain[0], chain


def _overlap(root: str, real_id: tuple, real_chain: list, store: str):
    """"contains" when ``root`` is the store or an ancestor of it, "inside" when it lies within it, else None."""
    resolved = os.path.realpath(root)
    ident = _identity(resolved, store)
    if ident is not None and ident in real_chain:
        return "contains"
    if real_id in _chain(resolved, store):
        return "inside"
    return None


def _registry_roots(registry: str, store: str) -> list:
    """The roots a ``lane-roots.tsv`` lists."""

    def unreadable(why: str) -> None:
        refuse("receipt_store_in_lane_root", f"{store} ({registry} unreadable: {why})")

    try:
        data = _read_bytes(registry, "lane_roots_unreadable")
    except Refusal:
        unreadable("not a regular file that can be opened")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        unreadable("not UTF-8")
    if not lines or lines[0] != LANE_ROOTS_HEADER:
        unreadable(f"the header is not {LANE_ROOTS_HEADER!r}")
    roots = []
    for number, line in enumerate(lines[1:], 2):
        fields = line.split("\t")
        if len(fields) != 4:
            unreadable(f"line {number} has {len(fields)} fields, not 4")
        if not fields[0].startswith("/"):
            unreadable(f"line {number} names a path that is not absolute")
        roots.append(fields[0])
    return roots


def _refuse_lane_root_at(ancestor: str, real_id: tuple, real_chain: list, store: str) -> None:
    """Refuse the store when ``ancestor`` (the store or a directory above it) is lane-writable."""
    name = os.path.basename(ancestor).casefold()
    if name.startswith("handback"):
        refuse("receipt_store_in_handback", f"{store} (a lane may write its hand-back directory)")
    if name == ".omp":
        refuse("receipt_store_in_lane_root", f"{store} (under .omp)")
    if _probe(os.path.join(ancestor, ".git"), store) is not None:
        refuse("receipt_store_in_lane_root", f"{store} (inside the git tree at {ancestor})")
    if (
        _probe(os.path.join(ancestor, "HEAD"), store) is not None
        and _probe(os.path.join(ancestor, "objects"), store) is not None
    ):
        refuse("receipt_store_in_lane_root", f"{store} (inside the git directory at {ancestor})")
    registry = os.path.join(ancestor, LANE_ROOTS)
    if _probe(registry, store) is not None:
        for root in _registry_roots(registry, store):
            where = _overlap(root, real_id, real_chain, store)
            if where == "contains":
                refuse("receipt_store_in_lane_root", f"{store} (under lane root {root})")
            if where == "inside":
                refuse("receipt_store_in_lane_root", f"{store} (lane root {root} lies inside the store)")
    try:
        names = sorted(os.listdir(ancestor))
    except OSError:
        refuse("receipt_store_lane_writable", f"{store} ({ancestor} unlistable)")
    for entry in names:
        if not entry.endswith(LANE_SETTINGS):
            continue
        settings = os.path.join(ancestor, entry)
        for root in lane_write_roots(settings, store):
            where = _overlap(root, real_id, real_chain, store)
            if where == "contains":
                refuse("receipt_store_lane_writable", f"{store} ({settings} lets a lane write {root})")
            if where == "inside":
                refuse(
                    "receipt_store_lane_writable",
                    f"{store} ({settings} lets a lane write {root}, inside the store)",
                )


def _refuse_lane_root_within(real: str, store: str) -> None:
    """Refuse the store when a directory below it is a hand-back, ``.omp``, or git directory or tree."""

    def unlistable(error):
        refuse("receipt_store_unreadable", str(getattr(error, "filename", None)))

    for root, dirs, files in os.walk(real, followlinks=False, onerror=unlistable):
        if root == real:
            continue
        entries = set(dirs) | set(files)
        name = os.path.basename(root).casefold()
        if name.startswith("handback"):
            refuse("receipt_store_in_handback", f"{store} (contains the hand-back directory {root})")
        if name in (".omp", ".git"):
            refuse("receipt_store_in_lane_root", f"{store} (contains {root})")
        if ".git" in entries:
            refuse("receipt_store_in_lane_root", f"{store} (contains the git tree at {root})")
        if "HEAD" in entries and "objects" in entries:
            refuse("receipt_store_in_lane_root", f"{store} (contains the git directory at {root})")


def _without_terminal_dots(path: str) -> str:
    """``path`` without the trailing ``/`` and ``/.`` components the kernel resolves through its final component."""
    while True:
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        elif len(path) > 2 and path.endswith("/."):
            path = path[:-2]
        else:
            return path


def _owned_and_private(status) -> bool:
    return status.st_uid == os.getuid() and not status.st_mode & 0o022


def check_store(store: str) -> str:
    """Refuse a store that overlaps a lane-writable root the reader can name; return its resolved path."""
    try:
        status = os.lstat(_without_terminal_dots(store))
    except OSError as exc:
        if exc.errno in ABSENT:
            refuse("receipt_store_missing", store)
        _unresolvable(store, store, exc)
    if stat.S_ISLNK(status.st_mode):
        refuse("receipt_store_symlink", store)
    if not stat.S_ISDIR(status.st_mode):
        refuse("receipt_store_missing", store)
    try:
        real = os.path.realpath(store, strict=True)
    except OSError as exc:
        _unresolvable(store, store, exc)
    real_id, real_chain = _store_identity(real, store)
    for ancestor in _lineage(real):
        _refuse_lane_root_at(ancestor, real_id, real_chain, store)
    _refuse_lane_root_within(real, store)
    if not _owned_and_private(os.stat(real)):
        refuse("receipt_store_permissions", f"{store} (must be the caller's and not group or other writable)")
    return real


def root_lines(path: str, reason: str, absolute: bool) -> list:
    """The roots one root-list file names, one per line; blank lines and ``#`` comments skipped.

    With ``absolute`` every root must be an absolute path; without it, a
    home-relative path with no empty, ``.`` or ``..`` segment. A missing,
    symlinked, unreadable or malformed file, or one naming no root, refuses
    as ``reason``.
    """

    def unreadable(why) -> None:
        refuse(reason, f"{path} ({why})")

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        unreadable(exc.strerror or exc)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            unreadable("not a regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        unreadable(exc.strerror or exc)
    finally:
        os.close(fd)
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        unreadable("not UTF-8")
    roots = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if absolute and not line.startswith("/"):
            unreadable(f"{line!r} is not an absolute path")
        if not absolute and (line.startswith("/") or any(part in ("", ".", "..") for part in line.split("/"))):
            unreadable(f"{line!r} is not a home-relative path")
        roots.append(line)
    if not roots:
        unreadable("names no root")
    return roots


def _refuse_in_roots(roots: list, real_id, real_chain: list, store: str, name: str, kind: str) -> None:
    for root in roots:
        where = _overlap(root, real_id, real_chain, store)
        if where == "contains":
            refuse(name, f"{store} (under {kind} {root})")
        if where == "inside":
            refuse(name, f"{store} ({kind} {root} lies inside the store)")


def _check_file(path: str, store: str, required: bool) -> None:
    """The database file, or its -wal or -shm file: a regular, single-link file of the caller's."""
    status = _probe(path, store)
    if status is None:
        if required:
            refuse("receipt_store_database_missing", path)
        return
    if stat.S_ISLNK(status.st_mode):
        refuse("receipt_store_symlink", path)
    if not stat.S_ISREG(status.st_mode):
        refuse("receipt_store_database_missing", f"{path} (not a regular file)")
    if status.st_nlink != 1:
        refuse("db_hardlinked", f"{path} ({status.st_nlink} links; a lane could rewrite it through another name)")
    if not _owned_and_private(status):
        refuse("receipt_store_permissions", f"{path} (must be the caller's and not group or other writable)")


def _check_documents(path: str, store: str) -> None:
    status = _probe(path, store)
    if status is None:
        return
    if stat.S_ISLNK(status.st_mode):
        refuse("receipt_store_symlink", path)
    if not stat.S_ISDIR(status.st_mode):
        refuse("receipt_store_documents_invalid", f"{path} (not a directory)")
    if not _owned_and_private(status):
        refuse("receipt_store_permissions", f"{path} (must be the caller's and not group or other writable)")


class StoreCheck:
    """Stage 1 over the paths the records port reports.

    The shipped check (``StoreCheck.shipped()``) reads the root lists from
    the package data, ``$TMPDIR`` from the process environment and the
    temp directory from ``tempfile.gettempdir``. A test builds one from
    Python with its own root-list files, environment and temp directory.
    """

    def __init__(
        self,
        temp_roots_file: str,
        sandbox_roots_file: str,
        *,
        environ: Mapping[str, str],
        gettempdir: Callable[[], str],
    ) -> None:
        self.temp_roots_file = str(temp_roots_file)
        self.sandbox_roots_file = str(sandbox_roots_file)
        self.environ = environ
        self.gettempdir = gettempdir

    @classmethod
    def shipped(cls) -> "StoreCheck":
        return cls(
            str(DATA_DIR / TEMP_ROOTS_FILE),
            str(DATA_DIR / SANDBOX_ROOTS_FILE),
            environ=os.environ,
            gettempdir=tempfile.gettempdir,
        )

    def temp_roots(self) -> list:
        """The listed temp roots, then ``$TMPDIR``, then the temp directory."""
        roots = root_lines(self.temp_roots_file, "receipt_temp_roots_unreadable", absolute=True)
        own = self.environ.get("TMPDIR")
        if own:
            roots.append(os.path.abspath(own))
        roots.append(self.gettempdir())
        return roots

    def _home(self) -> str:
        home = self.environ.get("HOME")
        if home is None:
            try:
                home = pwd.getpwuid(os.getuid()).pw_dir
            except KeyError:
                home = ""
        if not home:
            home = "/"
        return home

    def sandbox_roots(self) -> list:
        """The sandbox write roots under the caller's home directory, which must not be the filesystem root."""
        entries = root_lines(self.sandbox_roots_file, "receipt_sandbox_roots_unreadable", absolute=False)
        home = self._home()
        if not home.startswith("/"):
            refuse("receipt_sandbox_roots_unreadable", f"no absolute home directory ({home!r})")
        resolved = os.path.realpath(home)
        try:
            status = os.stat(resolved)
        except OSError as exc:
            if exc.errno not in ABSENT:
                refuse("receipt_sandbox_roots_unreadable", f"home directory {home!r} ({exc.strerror or exc})")
            status = None
        root = os.stat("/")
        if status is not None and (status.st_dev, status.st_ino) == (root.st_dev, root.st_ino):
            refuse("receipt_sandbox_roots_unreadable", f"home directory is the filesystem root ({home!r})")
        return [os.path.join(resolved, entry) for entry in entries]

    def check(self, paths: DatabasePaths) -> str:
        """The resolved store directory, or a refusal; any stray error refuses by name."""
        try:
            return self._check(paths)
        except Refusal:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            refuse("receipt_reader_failed", f"{type(exc).__name__}: {exc}")

    def _check(self, paths: DatabasePaths) -> str:
        database = getattr(paths, "database", None)
        documents = getattr(paths, "documents", None)
        for label, value in (("database", database), ("documents", documents)):
            if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                refuse("receipt_store_database_invalid", f"the records port's {label} path is {value!r}")
        name = os.path.basename(database)
        if name in ("", ".", ".."):
            refuse("receipt_store_database_invalid", f"{database!r} names no file")
        store = os.path.dirname(database)
        if os.path.dirname(documents) != store or os.path.basename(documents) in ("", ".", ".."):
            refuse("receipt_store_documents_invalid", f"{documents} is not beside {database}")
        real = check_store(store)
        real_id, real_chain = _store_identity(real, store)
        _refuse_in_roots(self.temp_roots(), real_id, real_chain, store, "receipt_store_in_temp_root", "temp root")
        _refuse_in_roots(
            self.sandbox_roots(), real_id, real_chain, store,
            "receipt_store_in_sandbox_root", "Claude sandbox write root",
        )
        base = os.path.join(real, name)
        _check_file(base, store, required=True)
        for suffix in ("-wal", "-shm"):
            _check_file(base + suffix, store, required=False)
        _check_documents(os.path.join(real, os.path.basename(documents)), store)
        return real
