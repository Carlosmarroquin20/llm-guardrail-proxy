"""Scan CLI — shift-left entry point.

``llm-guardrail-scan`` runs the same middleware pipeline the runtime
proxy uses, against a prompt loaded from disk, an argument, or stdin.
The exit code is the integration contract: ``0`` clean, ``1`` rejected,
``2`` input error. Pre-commit hooks and GitHub Actions reactors are
expected to react to those codes verbatim.

The CLI deliberately ignores ``GUARDRAIL_*`` environment variables.
A pre-commit hook whose behaviour drifts with the developer's shell
environment is one that produces unreproducible failures; flags are
the only configuration surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from llm_guardrail_proxy.cli.formatters import render_json, render_text
from llm_guardrail_proxy.core import ThresholdPolicy, TokenomicsService
from llm_guardrail_proxy.proxy.envelope import (
    ParsedPrompt,
    Provider,
    ProxyRequest,
)
from llm_guardrail_proxy.proxy.middleware import Middleware
from llm_guardrail_proxy.proxy.middlewares import (
    SecretScanMiddleware,
    TokenomicsMiddleware,
)
from llm_guardrail_proxy.proxy.pipeline import MiddlewarePipeline, PipelineDecision
from llm_guardrail_proxy.proxy.providers import (
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
)
from llm_guardrail_proxy.proxy.scanning import SecretScanner

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INPUT_ERROR = 2

# Placeholder envelope fields. The CLI does not forward upstream; these
# are surfaced only in audit-style annotations that the pipeline emits.
_CLI_PATH = "cli:scan"
_CLI_METHOD = "CLI"


# ----------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-guardrail-scan",
        description=(
            "Scan a prompt for secrets, PII, and tokenomics policy "
            "violations. Returns exit 0 on clean input, 1 when a "
            "guardrail rejects, 2 on input errors."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--file",
        type=Path,
        help=(
            "Path to a JSON file containing an OpenAI or Anthropic "
            "request body. Provider is auto-detected."
        ),
    )
    source.add_argument(
        "--text",
        type=str,
        help="Plain-text prompt to scan. Requires --model.",
    )

    parser.add_argument(
        "--model",
        type=str,
        default="gpt-4o",
        help=(
            "Model identifier (used for tokenomics and as the recorded "
            "model in --text / stdin mode). Default: gpt-4o."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format. Default: json.",
    )

    parser.add_argument(
        "--tokens",
        action="store_true",
        help="Enable the tokenomics check.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=100_000,
        help=(
            "Tokens upper bound when --tokens is enabled. The default "
            "(100k) is deliberately lax — shift-left checks should fail "
            "on definite policy violations, not on legitimate large "
            "prompts."
        ),
    )
    parser.add_argument(
        "--max-cost",
        type=str,
        default=None,
        help=(
            "Cost upper bound in USD when --tokens is enabled. Parsed "
            "as a Decimal. Omit to leave cost unrestricted."
        ),
    )

    parser.add_argument(
        "--pii",
        action="store_true",
        help=(
            "Enable PII scanning. Requires the [pii] extra and the "
            "spaCy English model."
        ),
    )

    return parser


# ----------------------------------------------------------------- input


def _detect_provider(payload: dict) -> Provider:
    """Best-effort provider detection from a parsed JSON body.

    The signal set is narrow on purpose: false detection is silent
    breakage. A top-level ``system`` field or a ``claude``-prefixed
    model name commit to Anthropic; everything else falls through to
    OpenAI Chat.
    """

    if "system" in payload:
        return Provider.ANTHROPIC
    model = payload.get("model")
    if isinstance(model, str) and model.lower().startswith("claude"):
        return Provider.ANTHROPIC
    return Provider.OPENAI


def _load_input(args: argparse.Namespace) -> ProxyRequest:
    """Build a :class:`ProxyRequest` from the CLI arguments.

    Three modes are accepted:

    * ``--file`` — JSON body parsed via the matching provider adapter.
    * ``--text`` — synthetic envelope with the supplied content.
    * neither — read stdin as plain text.

    Raises ``ValueError`` on malformed input; the caller maps it to the
    ``EXIT_INPUT_ERROR`` exit code.
    """

    if args.file is not None:
        try:
            raw = args.file.read_bytes()
        except OSError as exc:
            raise ValueError(f"could not read {args.file}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{args.file} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{args.file} must contain a JSON object")

        provider = _detect_provider(payload)
        adapter = (
            AnthropicMessagesAdapter()
            if provider is Provider.ANTHROPIC
            else OpenAIChatAdapter()
        )
        parsed = adapter.parse(raw)
        return ProxyRequest(
            path=_CLI_PATH,
            method=_CLI_METHOD,
            headers={},
            raw_body=raw,
            parsed=parsed,
        )

    # --text / stdin branches build a synthetic envelope. The placeholder
    # raw_body is a minimal OpenAI-shaped JSON so any downstream code path
    # that re-parses the body (e.g. the Mutate handler in the pipeline)
    # still operates on a well-formed document.
    if args.text is not None:
        content = args.text
    else:
        if sys.stdin.isatty():
            raise ValueError(
                "no input supplied — pass --file PATH, --text STRING, or pipe "
                "the prompt on stdin."
            )
        content = sys.stdin.read()

    synthetic_body = json.dumps(
        {
            "model": args.model,
            "messages": [{"role": "user", "content": content}],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    parsed = ParsedPrompt(
        provider=Provider.OPENAI,
        model=args.model,
        content=content,
    )
    return ProxyRequest(
        path="/v1/chat/completions",  # well-formed so the adapter
                                       # resolves cleanly if needed
        method=_CLI_METHOD,
        headers={},
        raw_body=synthetic_body,
        parsed=parsed,
    )


# ----------------------------------------------------------- middlewares


def _build_pipeline(args: argparse.Namespace) -> MiddlewarePipeline:
    """Construct the pipeline that matches the flags.

    Secret scanning is always present — it has no plausible false-
    positive surface against the curated catalogue. Tokenomics and PII
    are gated on explicit opt-in.
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


# ----------------------------------------------------------------- main


async def _run(request: ProxyRequest, pipeline: MiddlewarePipeline) -> PipelineDecision:
    return await pipeline.run(request)


def main(argv: Sequence[str] | None = None) -> int:
    """Programmatic entry point — returns an integer exit code.

    Separated from :func:`cli` so the test suite can drive the CLI by
    direct call instead of subprocess. The two paths share every line
    of business logic.
    """

    args = _build_parser().parse_args(argv)

    try:
        request = _load_input(args)
        pipeline = _build_pipeline(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    decision = asyncio.run(_run(request, pipeline))

    if args.format == "json":
        sys.stdout.write(render_json(decision))
    else:
        sys.stdout.write(render_text(decision))
    sys.stdout.write("\n")

    return EXIT_OK if decision.is_allowed else EXIT_REJECTED


def cli() -> None:
    """Console-script entry point — wraps :func:`main` with ``sys.exit``."""

    sys.exit(main())
