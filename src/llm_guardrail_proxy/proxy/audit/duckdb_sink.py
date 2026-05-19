"""DuckDB-backed audit sink.

DuckDB is selected over SQLite because the read-side use cases (Phase 4c's
``/stats`` endpoint, ad-hoc FinOps queries) lean heavily on aggregations
across millions of rows where DuckDB's vectorised execution outperforms
SQLite by an order of magnitude. The wire format is a single embedded
file, so operational surface stays minimal.

The sink uses one persistent connection guarded by an :class:`anyio.Lock`.
DuckDB connections are not safe for concurrent access; the lock serialises
writes, and the per-record latency stays well below a millisecond at the
volumes typical of a developer-facing proxy. A connection pool would be
the right answer at higher throughput; that is deferred.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio

from llm_guardrail_proxy.proxy.audit.records import AuditRecord


class MissingAuditBackend(RuntimeError):
    """Raised when ``duckdb`` is required but not installed."""


# Single-statement DDL kept in module scope so the test suite can assert
# that the schema actually applied to the database matches what the code
# claims it does.
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    request_id            VARCHAR PRIMARY KEY,
    timestamp             TIMESTAMPTZ NOT NULL,
    provider              VARCHAR NOT NULL,
    path                  VARCHAR NOT NULL,
    model                 VARCHAR NOT NULL,
    verdict               VARCHAR NOT NULL,
    rejecting_middleware  VARCHAR,
    reject_reason         VARCHAR,
    reject_status_code    INTEGER,
    token_count           INTEGER,
    estimated_cost_usd    DECIMAL(38, 18),
    mutations_applied     BOOLEAN NOT NULL,
    findings              JSON NOT NULL,
    latency_ms            DOUBLE NOT NULL,
    upstream_status_code  INTEGER,
    upstream_error        VARCHAR
)
""".strip()

_INSERT_SQL = """
INSERT INTO {table} (
    request_id, timestamp, provider, path, model, verdict,
    rejecting_middleware, reject_reason, reject_status_code,
    token_count, estimated_cost_usd, mutations_applied,
    findings, latency_ms, upstream_status_code, upstream_error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""".strip()


class DuckdbAuditSink:
    """Persist audit records to an embedded DuckDB database.

    Parameters
    ----------
    path:
        Filesystem location of the DuckDB file. Parent directories are
        created on demand. Use ``":memory:"`` for in-process testing.
    table:
        Table name. Customisable so multiple proxy deployments can share a
        database file without colliding.

    Raises
    ------
    MissingAuditBackend
        At construction time if ``duckdb`` is not importable.
    """

    __slots__ = ("_conn", "_insert_sql", "_lock", "_path", "_table")

    def __init__(self, path: str | Path, *, table: str = "audit_records") -> None:
        try:
            import duckdb  # local import — keeps duckdb a soft dependency
        except ImportError as exc:
            raise MissingAuditBackend(
                "DuckDB audit sink requires the 'duckdb' extra. "
                "Install with: pip install -e '.[duckdb]'"
            ) from exc

        if not table.replace("_", "").isalnum():
            raise ValueError(
                "table name must be alphanumeric or underscore "
                "(the literal is interpolated into DDL — no parameterisation)."
            )

        self._path: str = str(path)
        self._table: str = table
        self._lock = anyio.Lock()

        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = duckdb.connect(self._path)
        self._conn.execute(_CREATE_TABLE_SQL.format(table=table))
        self._insert_sql = _INSERT_SQL.format(table=table)

    @property
    def path(self) -> str:
        return self._path

    @property
    def table(self) -> str:
        return self._table

    async def record(self, entry: AuditRecord) -> None:
        payload = self._to_row(entry)
        async with self._lock:
            await anyio.to_thread.run_sync(self._execute_insert, payload)

    async def aclose(self) -> None:
        async with self._lock:
            await anyio.to_thread.run_sync(self._conn.close)

    # ----------------------------------------------------------- internals

    def _execute_insert(self, row: tuple[Any, ...]) -> None:
        self._conn.execute(self._insert_sql, list(row))

    @staticmethod
    def _to_row(entry: AuditRecord) -> tuple[Any, ...]:
        """Project an :class:`AuditRecord` onto the column ordering of
        ``_INSERT_SQL``.

        ``findings`` is serialised to a JSON string; DuckDB's JSON column
        accepts text and parses it on read. ``estimated_cost_usd`` is left
        as a :class:`Decimal` so the DECIMAL(38, 18) column stores it
        without float drift.
        """

        findings_json = json.dumps(
            [f.model_dump(mode="json") for f in entry.findings],
            ensure_ascii=False,
        )
        return (
            str(entry.request_id),
            entry.timestamp,
            entry.provider.value,
            entry.path,
            entry.model,
            entry.verdict.value,
            entry.rejecting_middleware,
            entry.reject_reason,
            entry.reject_status_code,
            entry.token_count,
            entry.estimated_cost_usd,
            entry.mutations_applied,
            findings_json,
            entry.latency_ms,
            entry.upstream_status_code,
            entry.upstream_error,
        )
