"""Module-level entry point — enables ``python -m llm_guardrail_proxy.cli``.

Kept thin: the real work lives in :mod:`scan`. This shim exists so the
CLI can be invoked even on environments where the console script is not
yet on ``PATH`` (e.g. immediately after ``pip install -e .`` on a
shell that has not refreshed its entry-point cache).
"""

from llm_guardrail_proxy.cli.scan import cli

if __name__ == "__main__":
    cli()
