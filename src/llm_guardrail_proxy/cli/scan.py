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

This module is the thin orchestrator. Concerns are delegated:

* :mod:`parser` — argparse setup.
* :mod:`inputs` — file/text/stdin → ProxyRequest envelopes.
* :mod:`runner` — pipeline construction + sequential execution.
* :mod:`formatters` — JSON / text rendering, single and batch shapes.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Sequence

from llm_guardrail_proxy.cli.formatters import (
    render_batch_json,
    render_batch_text,
    render_json,
    render_text,
)
from llm_guardrail_proxy.cli.inputs import load_inputs
from llm_guardrail_proxy.cli.parser import build_parser
from llm_guardrail_proxy.cli.runner import build_pipeline, run_all

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INPUT_ERROR = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Programmatic entry point — returns an integer exit code.

    Separated from :func:`cli` so the test suite can drive the CLI by
    direct call instead of subprocess. The two paths share every line
    of business logic.
    """

    args = build_parser().parse_args(argv)

    try:
        inputs = load_inputs(args)
        pipeline = build_pipeline(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    results = asyncio.run(run_all(pipeline, inputs))

    # Output shape is determined by *invocation mode*, not by the number
    # of resulting decisions. Pre-commit calls this CLI the same way
    # whether one or many files are staged; flipping the schema based
    # on count would force consumers to special-case the boundary.
    batch_invocation = bool(args.files)
    if batch_invocation:
        if args.format == "json":
            sys.stdout.write(render_batch_json(results))
        else:
            sys.stdout.write(render_batch_text(results))
    else:
        _, decision = results[0]
        if args.format == "json":
            sys.stdout.write(render_json(decision))
        else:
            sys.stdout.write(render_text(decision))
    sys.stdout.write("\n")

    rejected = any(not d.is_allowed for _, d in results)
    return EXIT_REJECTED if rejected else EXIT_OK


def cli() -> None:
    """Console-script entry point — wraps :func:`main` with ``sys.exit``."""

    sys.exit(main())
