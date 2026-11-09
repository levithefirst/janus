"""Storage backends. Everything Janus persists goes through one of these.

    FileBackend      files under JANUS_DATA_DIR (default; local development and tests)
    SqliteBackend    one SQLite file (JANUS_STORE=sqlite); a single-host fallback
    PostgresBackend  DATABASE_URL_UNPOOLED or DATABASE_URL (JANUS_STORE=postgres; the default when either is
                     set). Use this on Vercel: serverless functions have no persistent disk.

All three give the same guarantees, which the rest of Janus relies on:

- documents with a version number and a real compare-and-set write (`cas_doc`);
- `add`: create a key only if it does not exist yet, atomically (webhook event claims);
- append-only streams where the next entry is built from the last one under a lock (hash chains);
- blobs (photos);
- named exclusive locks that hold across threads and processes. On Postgres they are session advisory
  locks, so they also hold across serverless instances. They need a direct connection: a transaction-mode
  pooler (PgBouncer, Neon's "-pooler" host) does not keep session locks, which is why
  DATABASE_URL_UNPOOLED is preferred when it is set.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

Builder = Callable[[Optional[dict]], dict]


class Backend:
    name = "base"

    def get(self, key: str) -> Optional[bytes]: ...
    def put(self, key: str, data: bytes) -> None: ...
    def add(self, key: str, data: bytes = b"") -> bool: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str) -> list[str]: ...
    def get_doc(self, key: str) -> tuple[int, Optional[bytes]]: ...
    def cas_doc(self, key: str, expected_version: int, data: bytes) -> bool: ...
    def docs(self, prefix: str) -> list[bytes]: ...
    def stream(self, name: str) -> list[dict]: ...
    def append(self, name: str, build: Builder) -> dict: ...
    def lock(self, name: str): ...
    @property
    def local_dir(self) -> Optional[Path]:
        """A directory holding the blobs as plain files, if this backend has one."""
        return None


def _safe(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\x00" in key:
        raise ValueError(f"bad key {key!r}")
    return key


# ---------------------------------------------------------------- files

class FileBackend(Backend):
    name = "file"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._tlocks: dict[str, threading.Lock] = {}
        self._depth: dict[tuple[int, str], int] = {}

    @property
    def local_dir(self) -> Path:
        return self.root

    def _p(self, key: str) -> Path:
        return self.root / _safe(key)

    def get(self, key):
        p = self._p(key)
        return p.read_bytes() if p.is_file() else None

    def put(self, key, data):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def add(self, key, data=b""):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return True

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)

    def keys(self, prefix):
        base = self.root
        return sorted(str(p.relative_to(base)) for p in base.glob(_safe(prefix) + "**/*")
                      if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".lock"))

    # documents: "<version>\n<bytes>"
    def get_doc(self, key):
        raw = self.get(key)
        if raw is None:
            return 0, None
        head, _, body = raw.partition(b"\n")
        return int(head), body

    def cas_doc(self, key, expected_version, data):
        with self.lock(f"doc:{key}"):
            current, _ = self.get_doc(key)
            if current != expected_version:
                return False
            self.put(key, f"{expected_version + 1}\n".encode() + data)
            return True

    def docs(self, prefix):
        out = []
        for p in sorted(self.root.glob(_safe(prefix) + "*/doc.json")):
            out.append(p.read_bytes().partition(b"\n")[2])
        return out

    def stream(self, name):
        p = self._p(f"{name}.jsonl")
        if not p.is_file():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def append(self, name, build):
        with self.lock(f"stream:{name}"):
            entries = self.stream(name)
            entry = build(entries[-1] if entries else None)
            p = self._p(f"{name}.jsonl")
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            return entry

    @contextmanager
    def lock(self, name) -> Iterator[None]:
        """flock on a per-name file, re-entrant within one thread (a second flock on a new fd would deadlock)."""
        me = (threading.get_ident(), name)
        with self._guard:
            depth = self._depth.get(me, 0)
            if depth:
                self._depth[me] = depth + 1
        if depth:
            try:
                yield
            finally:
                with self._guard:
                    self._depth[me] -= 1
            return
        with self._guard:
            tl = self._tlocks.setdefault(name, threading.Lock())
        d = self.root / "locks"
        d.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(name.encode()).hexdigest()[:32]
        with tl, (d / f"{digest}.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self._guard:
                self._depth[me] = 1
            try:
                yield
            finally:
                with self._guard:
                    self._depth.pop(me, None)
                fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- SQL (shared by SQLite and Postgres)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS janus_kv (k TEXT PRIMARY KEY, v {blob} NOT NULL)",
    "CREATE TABLE IF NOT EXISTS janus_docs (k TEXT PRIMARY KEY, version INTEGER NOT NULL, v {blob} NOT NULL)",
    "CREATE TABLE IF NOT EXISTS janus_streams (stream TEXT NOT NULL, seq INTEGER NOT NULL, v TEXT NOT NULL, "
    "PRIMARY KEY (stream, seq))",
]


class _SqlBackend(Backend):
    blob = "BLOB"
    ph = "?"

    def _exec(self, sql: str, args: tuple = (), fetch: str = "") -> object: ...

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.ph)

    def get(self, key):
        row = self._exec("SELECT v FROM janus_kv WHERE k = ?", (_safe(key),), "one")
        return bytes(row[0]) if row else None

    def put(self, key, data):
        self._exec("INSERT INTO janus_kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
                   (_safe(key), data))

    def add(self, key, data=b""):
        return bool(self._exec("INSERT INTO janus_kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO NOTHING",
                               (_safe(key), data), "rowcount"))

    def delete(self, key):
        self._exec("DELETE FROM janus_kv WHERE k = ?", (_safe(key),))

    def keys(self, prefix):
        rows = self._exec("SELECT k FROM janus_kv WHERE k LIKE ? ORDER BY k", (_safe(prefix) + "%",), "all")
        return [r[0] for r in rows]

    def get_doc(self, key):
        row = self._exec("SELECT version, v FROM janus_docs WHERE k = ?", (_safe(key),), "one")
        return (row[0], bytes(row[1])) if row else (0, None)

    def cas_doc(self, key, expected_version, data):
        if expected_version == 0:
            return bool(self._exec("INSERT INTO janus_docs (k, version, v) VALUES (?, 1, ?) ON CONFLICT (k) DO NOTHING",
                                   (_safe(key), data), "rowcount"))
        return bool(self._exec("UPDATE janus_docs SET v = ?, version = version + 1 WHERE k = ? AND version = ?",
                               (data, _safe(key), expected_version), "rowcount"))

    def docs(self, prefix):
        rows = self._exec("SELECT v FROM janus_docs WHERE k LIKE ? ORDER BY k", (_safe(prefix) + "%",), "all")
        return [bytes(r[0]) for r in rows]

    def stream(self, name):
        rows = self._exec("SELECT v FROM janus_streams WHERE stream = ? ORDER BY seq", (name,), "all")
        return [json.loads(r[0]) for r in rows]

    def append(self, name, build):
        with self.lock(f"stream:{name}"):
            row = self._exec("SELECT seq, v FROM janus_streams WHERE stream = ? ORDER BY seq DESC LIMIT 1",
                             (name,), "one")
            entry = build(json.loads(row[1]) if row else None)
            seq = (row[0] if row else 0) + 1
            self._exec("INSERT INTO janus_streams (stream, seq, v) VALUES (?, ?, ?)",
                       (name, seq, json.dumps(entry, sort_keys=True, default=str)))
            return entry


class SqliteBackend(_SqlBackend):
    """Single-file fallback. Locks use flock on a sidecar file, so they only hold on one host."""
    name = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._files = FileBackend(self.path.parent / f".{self.path.name}.locks")
        self._local = threading.local()
        for stmt in SCHEMA:
            self._exec(stmt.format(blob=self.blob))

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def _exec(self, sql, args=(), fetch=""):
        cur = self._conn().execute(self._q(sql), args)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.rowcount

    def lock(self, name):
        return self._files.lock(name)


class PostgresBackend(_SqlBackend):
    name = "postgres"
    blob = "BYTEA"
    ph = "%s"

    def __init__(self, url: str, schema: Optional[str] = None):
        import psycopg
        self._psycopg = psycopg
        self.url, self.schema = url, schema
        self._local = threading.local()
        self._guard = threading.Lock()
        self._held: dict[str, tuple[object, int]] = {}  # lock name -> (connection, depth) for re-entry
        if schema:
            with psycopg.connect(url, autocommit=True) as c:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for stmt in SCHEMA:
            self._exec(stmt.format(blob=self.blob))

    def _connect(self):
        c = self._psycopg.connect(self.url, autocommit=True)
        if self.schema:
            c.execute(f'SET search_path TO "{self.schema}"')
        return c

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            c = self._connect()
            self._local.conn = c
        return c

    def _exec(self, sql, args=(), fetch=""):
        cur = self._conn().execute(self._q(sql), args)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.rowcount

    @contextmanager
    def lock(self, name) -> Iterator[None]:
        key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
        me = (threading.get_ident(), name)
        with self._guard:
            held = self._held.get(me)
        if held:  # re-entered by the same thread
            self._held[me] = (held[0], held[1] + 1)
            try:
                yield
            finally:
                c, depth = self._held[me]
                self._held[me] = (c, depth - 1)
            return
        c = self._connect()  # a dedicated session holds the advisory lock
        try:
            c.execute("SELECT pg_advisory_lock(%s)", (key,))
            self._held[me] = (c, 1)
            try:
                yield
            finally:
                self._held.pop(me, None)
                c.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            c.close()


def from_config(config) -> Backend:
    kind = config.store
    if kind == "postgres":
        if not config.database_url:
            raise ValueError("JANUS_STORE=postgres needs DATABASE_URL_UNPOOLED or DATABASE_URL")
        return PostgresBackend(config.database_url)
    if kind == "sqlite":
        return SqliteBackend(Path(config.sqlite_path or Path(config.data_dir) / "janus.db"))
    return FileBackend(Path(config.data_dir))
