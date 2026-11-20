"""Database access: a connection pool for the app, and a tiny migration runner.

Run migrations with: python -m app.db
"""

from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

# backend/migrations. Files run in filename order, so names are zero-padded (001_, 002_, ...).
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def create_pool() -> AsyncConnectionPool:
    """Pool shared by request handlers and worker tasks. The caller opens and closes it."""
    return AsyncConnectionPool(
        settings.database_url,
        kwargs={
            # dict_row so queries return {"column": value}, which maps onto pydantic models.
            "row_factory": dict_row,
            # Each statement commits on its own; multi-statement writes use an explicit
            # conn.transaction(). Saves a BEGIN/COMMIT round trip on every single-statement
            # query, which matters for the worker's idle polling.
            "autocommit": True,
            # No server-side prepared statements: Neon's pooled endpoint is PgBouncer in
            # transaction mode, where a statement prepared on one server connection may be
            # missing on the next. The cost is re-planning each query, negligible here.
            "prepare_threshold": None,
        },
        # Neon closes idle connections, so test each one before handing it out; otherwise the
        # first request after a quiet period gets a dead connection and a 500.
        check=AsyncConnectionPool.check_connection,
        # Small: Neon caps connections, and the API and worker each hold a pool against that cap.
        min_size=1,
        # A handful covers this app's concurrency; requests beyond it wait briefly for a slot.
        max_size=5,
        open=False,
    )


def run_migrations(database_url: str = settings.database_url) -> list[str]:
    """Apply every migrations/*.sql file not yet recorded in schema_migrations.

    Safe to run repeatedly: already-applied files are skipped. Each file runs in one
    transaction together with its bookkeeping row, so a failing file leaves no partial state.
    Meant to run once per deploy (not from every process at startup), so there is no locking.
    """
    applied_now = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        already_applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in already_applied:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    applied = run_migrations()
    print(f"Applied: {', '.join(applied)}" if applied else "No pending migrations.")
