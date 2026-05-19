"""Output renderers for the scan CLI.

Two formats are shipped: ``json`` (the default, intended for CI pipes
into ``jq`` or further processing) and ``text`` (intended for a human
reading commit-time output). They project the same underlying
:class:`PipelineDecision` onto different surface forms; no business
logic lives here.
"""

from __future__ import annotations

import json
from typing import Any

from llm_guardrail_proxy.proxy.envelope import Reject
from llm_guardrail_proxy.proxy.pipeline import PipelineDecision


def render_json(decision: PipelineDecision) -> str:
    """Emit a deterministic JSON document describing the decision.

    The shape is stable across releases so downstream tooling can parse
    it without version detection. Fields:

    * ``verdict`` — ``"allowed"`` or ``"rejected"``.
    * ``rejecting_middleware`` — string or null.
    * ``reject`` — full Reject payload when applicable, else null.
    * ``annotations`` — per-middleware annotation map verbatim.
    """

    payload: dict[str, Any] = {
        "verdict": "allowed" if decision.is_allowed else "rejected",
        "rejecting_middleware": decision.rejecting_middleware,
        "annotations": decision.annotations,
        "reject": _serialise_reject(decision),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def render_text(decision: PipelineDecision) -> str:
    """Emit a multi-line, human-readable summary.

    Format intentionally resembles a linter or test-runner output: a
    one-line verdict followed by indented finding details. No colour
    codes — terminals without ANSI support would render the bytes
    literally and pollute commit-time logs.
    """

    lines: list[str] = []
    if decision.is_allowed:
        lines.append("guardrail: PASS")
    else:
        assert isinstance(decision.outcome, Reject)
        lines.append(
            f"guardrail: FAIL ({decision.rejecting_middleware}: "
            f"{decision.outcome.reason})"
        )
        lines.append(f"  {decision.outcome.detail}")

    # Append per-middleware findings, regardless of verdict, so an
    # ``allowed`` run still surfaces tokenomics figures.
    for mw, payload in decision.annotations.items():
        summary = _annotation_summary(payload)
        if summary:
            lines.append(f"  [{mw}] {summary}")
        for finding in payload.get("findings", []) or []:
            lines.append(
                f"    - {finding.get('label', finding.get('kind'))}"
                f" severity={finding.get('severity')}"
                f" preview={finding.get('preview')}"
            )
    return "\n".join(lines)


def _serialise_reject(decision: PipelineDecision) -> dict[str, Any] | None:
    if not isinstance(decision.outcome, Reject):
        return None
    return {
        "status_code": decision.outcome.status_code,
        "reason": decision.outcome.reason,
        "detail": decision.outcome.detail,
    }


def _annotation_summary(payload: Any) -> str:
    """Extract a short ``key=value`` projection of an annotation payload.

    Used by the text renderer to display tokenomics figures and finding
    counts inline. Non-trivial values (lists, dicts) are summarised
    rather than rendered — the JSON format is the right surface for
    full detail.
    """

    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("token_count", "estimated_cost_usd", "finding_count"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    return " ".join(parts)
