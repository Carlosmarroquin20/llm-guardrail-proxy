"""Pipeline construction and execution for the scan CLI.

Two concerns live here: assembling the middleware chain that matches
the requested flags (:func:`build_pipeline`), and driving it against
every loaded input (:func:`run_all`). Output rendering belongs in
:mod:`formatters`; input loading belongs in :mod:`inputs`. Keeping
each step in its own module mirrors the proxy-side split between
``handler``, ``pipeline``, and ``forwarder``.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.envelope import ProxyRequest
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline, PipelineDecision
from llm_guardrail_proxy.proxy.scanning import SecretScanner


def build_pipeline(args: argparse.Namespace) -> MiddlewarePipeline:
    """Construct the pipeline that matches the flags.

    Secret scanning is always present — it has no plausible false-
    positive surface against the curated catalogue. Tokenomics and PII
    are gated on explicit opt-in.

    Raises :class:`ValueError` when ``--max-cost`` cannot be parsed as a
    Decimal, and :class:`RuntimeError` when ``--pii`` is requested but
    the optional ``[pii]`` extra is not installed.
    """

    middlewares: list[Middleware] = [
        SecretScanMiddleware(scanner=SecretScanner())
    ]

    if args.pii:
        try:
            from llm_guardrail_proxy.proxy.middlewares import PiiPolicy, PiiScanMiddleware
            from llm_guardrail_proxy.proxy.scanning import PiiScanner
        except ImportError as exc:  # pragma: no cover - import guard only
            raise RuntimeError(
                "PII scanning requires the [pii] extra. "
                "Install with: pip install 'llm-guardrail-proxy[pii]'"
            ) from exc
        middlewares.append(
            PiiScanMiddleware(scanner=PiiScanner(), policy=PiiPolicy.BLOCK)
        )

    if args.tokens:
        max_cost: Decimal | None = None
        if args.max_cost is not None:
            try:
                max_cost = Decimal(args.max_cost)
            except InvalidOperation as exc:
                raise ValueError(
                    f"--max-cost must be a decimal value, got {args.max_cost!r}"
                ) from exc
        policy = ThresholdPolicy(
            max_tokens=args.max_tokens,
            max_cost_usd=max_cost,
        )
        middlewares.append(
            TokenomicsMiddleware(service=TokenomicsService(), policy=policy)
        )

    return MiddlewarePipeline(middlewares)


async def run_all(
    pipeline: MiddlewarePipeline,
    requests: list[tuple[str, ProxyRequest]],
) -> list[tuple[str, PipelineDecision]]:
    """Run the pipeline against every (label, envelope) pair sequentially.

    Sequential rather than concurrent on purpose: ``tiktoken`` is
    CPU-bound, so concurrent runs do not speed anything up in the
    single-process CLI, and serial execution makes the output order
    match the input order — the property pre-commit expects.
    """

    results: list[tuple[str, PipelineDecision]] = []
    for label, req in requests:
        results.append((label, await pipeline.run(req)))
    return results
