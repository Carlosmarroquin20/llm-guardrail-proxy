"""Command-line interface — Phase 5 base.

The CLI is the shift-left counterpart of the runtime proxy: the same
guardrail pipeline applied to a prompt *file* (or stdin) instead of an
HTTP request, with an exit code that pre-commit hooks and CI jobs can
react to.

Defaults differ deliberately from the server defaults. The server runs
against live traffic and can afford to reject aggressively; a CI gate
that false-positives is one that developers learn to bypass. So:

* ``secret_scan`` is the only check enabled by default — its precision
  is essentially perfect.
* ``tokenomics`` and ``pii_scan`` are opt-in via flags.
"""

from llm_guardrail_proxy.cli.scan import cli, main

__all__ = ["cli", "main"]
