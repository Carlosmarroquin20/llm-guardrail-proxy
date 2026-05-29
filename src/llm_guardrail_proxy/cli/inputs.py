"""Input resolution for the scan CLI.

Translates parsed arguments into ``[(label, ProxyRequest), ...]`` pairs
the runner can execute against. Three modes are supported, and they
have exactly one in common: each builds a frozen ``ProxyRequest``
envelope that the rest of the pipeline treats uniformly.

* ``--file`` or positional paths — JSON body parsed via the matching
  provider adapter.
* ``--text`` — synthetic envelope with the supplied content.
* stdin — same as ``--text`` but the prompt is read from the pipe.

All error paths raise :class:`ValueError` with a user-readable message;
the caller maps it to the CLI's ``EXIT_INPUT_ERROR`` exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from llm_guardrail_proxy.proxy.envelope import (
    ParsedPrompt,
    Provider,
    ProxyRequest,
)
from llm_guardrail_proxy.proxy.providers import (
    AnthropicMessagesAdapter,
    OpenAIChatAdapter,
)

# Placeholder envelope fields. The CLI does not forward upstream; these
# are surfaced only in audit-style annotations that the pipeline emits.
_CLI_PATH = "cli:scan"
_CLI_METHOD = "CLI"


def detect_provider(payload: dict) -> Provider:
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


def load_file(path: Path) -> ProxyRequest:
    """Parse a single JSON-shaped prompt file into an envelope.

    Raises :class:`ValueError` with a user-readable message on any read
    or parse error.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")

    provider = detect_provider(payload)
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


def load_inputs(args: argparse.Namespace) -> list[tuple[str, ProxyRequest]]:
    """Resolve CLI flags into ``[(label, envelope), ...]``.

    ``label`` is the human-facing identifier displayed in batch output —
    a path for file-mode entries, ``-`` for stdin, ``--text`` for the
    literal-string mode. Single-mode invocations return a one-element
    list; the rest of the pipeline does not need to special-case batch.

    The three input sources (positional, ``--file``, ``--text``/stdin)
    are mutually exclusive. The parser already enforces ``--file`` vs
    ``--text`` via :meth:`argparse.add_mutually_exclusive_group`; the
    positional-vs-flag conflict is checked here because the parser
    cannot express the three-way constraint natively.
    """

    if args.files:
        if args.file is not None or args.text is not None:
            raise ValueError(
                "positional file arguments are mutually exclusive with "
                "--file and --text."
            )
        return [(str(path), load_file(path)) for path in args.files]

    if args.file is not None:
        return [(str(args.file), load_file(args.file))]

    # --text / stdin branches build a synthetic envelope. The placeholder
    # raw_body is a minimal OpenAI-shaped JSON so any downstream code path
    # that re-parses the body (e.g. the Mutate handler in the pipeline)
    # still operates on a well-formed document.
    if args.text is not None:
        content = args.text
        label = "--text"
    else:
        if sys.stdin.isatty():
            raise ValueError(
                "no input supplied — pass --file PATH, --text STRING, a list "
                "of positional file paths, or pipe the prompt on stdin."
            )
        content = sys.stdin.read()
        label = "-"

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
    return [
        (
            label,
            ProxyRequest(
                path="/v1/chat/completions",  # well-formed so the
                                              # adapter resolves cleanly
                method=_CLI_METHOD,
                headers={},
                raw_body=synthetic_body,
                parsed=parsed,
            ),
        )
    ]
