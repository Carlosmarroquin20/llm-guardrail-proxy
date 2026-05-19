"""structlog-backed audit sink and process-wide logging configuration.

The sink emits one structured log event per audit record. The event name
is stable (``audit.request``) so log shippers can route on it without
reading the body. The fields mirror :class:`AuditRecord` so an operator
inspecting logs sees the same data the persistent ledger holds.

``configure_logging`` is the one-call setup for the proxy's structlog
stack. It is idempotent: calling it twice (which uvicorn lifespan plus
tests routinely does) does not stack processors.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from llm_guardrail_proxy.proxy.audit.records import AuditRecord

_DEFAULT_LOGGER_NAME = "llm_guardrail_proxy.audit"
_CONFIGURED: bool = False


def configure_logging(*, json: bool = True, level: int = logging.INFO) -> None:
    """Install a single, deterministic structlog configuration.

    Parameters
    ----------
    json:
        When ``True`` (production default), events are rendered as JSON.
        When ``False`` (interactive development), structlog's
        :class:`ConsoleRenderer` is used, which produces a human-readable
        coloured stream.
    level:
        Stdlib log level for the underlying root logger. structlog itself
        does not filter; this controls the stdlib bridge.

    The function is idempotent. Subsequent calls replace the previous
    configuration so test suites and lifespan hooks can both invoke it
    without producing duplicate emissions.
    """

    global _CONFIGURED

    # Stdlib bridge — structlog can render events itself, but a handler
    # is still needed so the bytes reach stdout.
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def _record_payload(record: AuditRecord) -> dict[str, Any]:
    """Project an :class:`AuditRecord` onto the keyword arguments structlog
    expects.

    ``model_dump(mode="json")`` is used so Decimal, datetime, and UUID are
    rendered identically to the JSONL sink — the log line and the ledger
    row carry the same byte-level representation.
    """

    return record.model_dump(mode="json")


class LoggingAuditSink:
    """Emit each audit record as a structlog event.

    Parameters
    ----------
    logger_name:
        Logger to bind. Defaults to ``llm_guardrail_proxy.audit`` so
        operators can route audit events independently from generic
        application logs (e.g. an OTel collector route on logger name).
    """

    __slots__ = ("_logger",)

    def __init__(self, *, logger_name: str = _DEFAULT_LOGGER_NAME) -> None:
        self._logger = structlog.get_logger(logger_name)

    async def record(self, entry: AuditRecord) -> None:
        # ``audit.request`` is the stable event identifier; downstream
        # systems should pivot on it rather than on the human-facing
        # message text, which is permitted to evolve.
        self._logger.info("audit.request", **_record_payload(entry))

    async def aclose(self) -> None:  # noqa: D401
        return None
