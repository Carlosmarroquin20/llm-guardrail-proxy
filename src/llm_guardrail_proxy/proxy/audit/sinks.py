"""Audit sinks.

Three implementations are shipped with the base:

* :class:`NullAuditSink` — discards everything. Used when auditing is
  explicitly disabled.
* :class:`InMemoryAuditSink` — bounded deque. The default for tests and
  for development where filesystem permissions are not guaranteed.
* :class:`JsonlAuditSink` — append-only newline-delimited JSON file.
  Crash-safe in the sense that each record is flushed independently;
  partial writes at process kill are limited to the last in-flight line.

Phase 4b will register a DuckDB-backed sink alongside these without
modifying the route handler — that is the whole point of the Protocol.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

import anyio

from llm_guardrail_proxy.proxy.audit.records import AuditRecord


@runtime_checkable
class AuditSink(Protocol):
    """Asynchronous, append-only audit destination.

    ``aclose`` exists so file-backed or socket-backed sinks can flush
    buffers cleanly at shutdown. In-memory implementations make it a
    no-op. Implementations must be safe to call concurrently from
    multiple ASGI worker tasks — sinks operating on shared resources
    own their own locking.
    """

    async def record(self, entry: AuditRecord) -> None: ...

    async def aclose(self) -> None: ...


class NullAuditSink:
    """Discard every record. Used when ``audit_enabled`` is False."""

    async def record(self, entry: AuditRecord) -> None:  # noqa: D401
        return None

    async def aclose(self) -> None:  # noqa: D401
        return None


class InMemoryAuditSink:
    """Bounded ring buffer of recent records.

    The deque is intentionally lossy: when the buffer fills, oldest
    records are evicted. This is the right semantics for a development
    sink (Phase 4c's stats endpoint shows ``last N`` requests) and for
    tests, where the suite cares about the *most recent* request.

    Parameters
    ----------
    capacity:
        Maximum number of records retained. Must be a positive integer.
    """

    __slots__ = ("_buffer", "_lock")

    def __init__(self, *, capacity: int = 1000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._buffer: deque[AuditRecord] = deque(maxlen=capacity)
        self._lock = anyio.Lock()

    async def record(self, entry: AuditRecord) -> None:
        async with self._lock:
            self._buffer.append(entry)

    async def aclose(self) -> None:  # noqa: D401
        return None

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        """Snapshot of the current buffer contents.

        Synchronous on purpose: read-side consumers (the stats endpoint,
        tests) want a cheap, lock-free view. The returned tuple is a
        copy, so concurrent appends after the read cannot mutate it.
        """

        return tuple(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)

    def __iter__(self) -> Iterable[AuditRecord]:  # type: ignore[override]
        return iter(tuple(self._buffer))


class JsonlAuditSink:
    """Append-only newline-delimited JSON file sink.

    Each record is serialised via ``AuditRecord.model_dump_json`` and
    written as a single line. Writes are serialised through an
    :class:`anyio.Lock` so concurrent ASGI workers cannot interleave
    bytes within a record. The file handle is opened-and-closed per
    record — at the volumes typical of a developer-facing proxy this
    is cheaper than the alternative (a long-lived handle that needs
    coordinated flushing) and is crash-safe.

    Parameters
    ----------
    path:
        Filesystem location of the JSONL file. Parent directories are
        created on demand to keep deployment ergonomics simple.
    """

    __slots__ = ("_lock", "_path")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = anyio.Lock()

    async def record(self, entry: AuditRecord) -> None:
        line = entry.model_dump_json() + "\n"
        async with self._lock:
            await anyio.to_thread.run_sync(self._append, line)

    async def aclose(self) -> None:  # noqa: D401
        return None

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, line: str) -> None:
        # Open in append mode so multiple processes pointed at the same
        # file accumulate records safely (POSIX append is atomic for
        # writes ≤ PIPE_BUF; well within our record size).
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)
