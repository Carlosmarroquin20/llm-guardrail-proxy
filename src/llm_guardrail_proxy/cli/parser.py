"""Argument parser for the scan CLI.

Lives in its own module because the parser setup is largely declarative
metadata that rarely changes for the same reasons the rest of the CLI
changes. Splitting it keeps :mod:`scan` focused on orchestration.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Construct the ``llm-guardrail-scan`` argument parser.

    The structure encodes one invariant that is easy to lose in a
    refactor: ``--file``, ``--text``, and positional file paths are
    mutually exclusive input sources, but the parser cannot express the
    three-way constraint natively (only the first two are in a mutex
    group). The positional/flag conflict is checked at input-loading
    time; see :func:`cli.inputs.load_inputs`.
    """

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

    # Positional file paths support pre-commit's standard invocation,
    # which appends the list of staged files to the hook entry. The
    # ``nargs="*"`` form combined with the mutually-exclusive group
    # above means a single invocation always has exactly one input
    # source: --file, --text, stdin, or a list of paths.
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        metavar="FILE",
        help=(
            "Files to scan in batch mode (the form pre-commit uses). "
            "Mutually exclusive with --file/--text/stdin. Provider is "
            "auto-detected per file."
        ),
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
