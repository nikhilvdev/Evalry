"""Plain-SQL migrations: numbered files in ``evalry/migrations``, applied in order, each in its own transaction.

Applied files are recorded with a checksum; if an applied file is later edited, ``migrate`` refuses to continue, because the
database no longer matches what the file says. An advisory lock stops two deploys migrating at once.
"""

import hashlib
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_ID = 7_461_922_001  # arbitrary constant shared by every Evalry migrator


class MigrationError(Exception):
    """A migration cannot be applied safely. The message says what to do."""


def _files() -> list[tuple[str, str]]:
    return [(p.name, p.read_text()) for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))]


def migrate(admin_dsn: str) -> list[str]:
    """Apply pending migrations as the schema owner. Returns the names applied this call."""
    applied_now: list[str] = []
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done: dict[str, str] = {
                row[0]: row[1]
                for row in conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
            }
            files = _files()
            known = {name for name, _ in files}
            if missing := sorted(set(done) - known):
                raise MigrationError(
                    f"The database has migrations this build does not know: {missing}. Deploy a newer build or restore the files."
                )
            for name, sql in files:
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if name in done:
                    if done[name] != checksum:
                        raise MigrationError(
                            f"{name} was edited after it was applied. Restore it and add a new numbered migration instead."
                        )
                    continue
                with conn.transaction():
                    conn.execute(sql)  # our own packaged file, never user input
                    conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (name, checksum)
                    )
                applied_now.append(name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now
