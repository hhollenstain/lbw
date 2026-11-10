"""SQLite for a local install, Postgres when LBW_DATABASE_URL points at one.

Each store root keeps its own SQLite file at `.lbw/lbw.db`. A database URL
uses that server instead, so a self-hosted or hosted install can share one
transactional database. The schema is the same either way.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_lbw.runtime.project import ensure_run_gitignore


class Database:
    """One connection. SQL uses `?` placeholders on both backends."""

    def __init__(self, raw: Any, kind: str) -> None:
        self.raw = raw
        self.kind = kind

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        if self.kind == "postgres":
            statement = sql.replace("%", "%%").replace("?", "%s")
        else:
            statement = sql
        cursor = self.raw.cursor()
        cursor.execute(statement, params)
        return cursor

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def database_configured(root: Path, database_url: str = "") -> bool:
    """True when a database file or a Postgres URL is already there to read."""
    url = (database_url or "").strip()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        return True
    try:
        path = sqlite_path(root, database_url)
    except ValueError:
        return False
    return path is not None and path.is_file()


def sqlite_path(root: Path, database_url: str = "") -> Path | None:
    """The local database file, or None when this install uses Postgres."""
    url = database_url.strip()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        return None
    if not url:
        return Path(root) / ".lbw" / "lbw.db"
    if not url.startswith("sqlite:"):
        raise ValueError("database url must be sqlite or postgres")
    body = url.removeprefix("sqlite:")
    if not body.startswith("///"):
        raise ValueError("sqlite url must look like sqlite:///lbw.db or sqlite:////tmp/lbw.db")
    path = Path(body[3:])
    if not path.is_absolute():
        path = Path(root) / path
    return path


_SCHEMA_LOCK = 834271901


@contextmanager
def database(root: Path, database_url: str = "") -> Iterator[Database]:
    db = _open(Path(root), database_url)
    try:
        ensure_schema(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_schema(db: Database) -> None:
    """Apply the schema once. Later requests only read the saved version.

    Running the migration on every request deadlocks Postgres: one connection
    holds a lock from CREATE INDEX while another waits to alter a table, and
    the first waits for a lock the second already holds.
    """
    from agent_lbw.runtime.store.schema import (
        SCHEMA_VERSION,
        apply_schema,
        read_schema_version,
        write_schema_version,
    )

    for attempt in range(3):
        try:
            _ensure_schema_once(
                db,
                SCHEMA_VERSION,
                read_schema_version,
                write_schema_version,
                apply_schema,
            )
            return
        except Exception as exc:
            db.rollback()
            if db.kind != "postgres" or "deadlock" not in str(exc).lower() or attempt == 2:
                raise


def _ensure_schema_once(db, version, read_version, write_version, migrate) -> None:
    if read_version(db) >= version:
        return
    locked = False
    if db.kind == "postgres":
        db.execute("SELECT pg_advisory_lock(?)", (_SCHEMA_LOCK,))
        locked = True
    try:
        if read_version(db) >= version:
            return
        migrate(db)
        write_version(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if locked:
            db.execute("SELECT pg_advisory_unlock(?)", (_SCHEMA_LOCK,))


def _open(root: Path, database_url: str) -> Database:
    url = database_url.strip()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        return _open_postgres(url)
    path = sqlite_path(root, url)
    if path is None:
        raise ValueError("database url must be sqlite or postgres")
    ensure_run_gitignore(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, timeout=5)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Database(raw, "sqlite")


def _open_postgres(url: str) -> Database:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise ValueError("Postgres support needs psycopg") from exc
    raw = psycopg.connect(url, row_factory=dict_row)
    return Database(raw, "postgres")
