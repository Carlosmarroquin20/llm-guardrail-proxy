"""Entry point for ``python -m llm_guardrail_proxy`` and the console script.

Kept deliberately thin: anything more elaborate than process bootstrap
belongs in :mod:`llm_guardrail_proxy.proxy.app`, where it can be
exercised by the in-process test suite.
"""

from __future__ import annotations

import uvicorn

from llm_guardrail_proxy.proxy.settings import ProxySettings


def main() -> None:
    """Launch the proxy under uvicorn using environment-resolved settings."""

    settings = ProxySettings()
    uvicorn.run(
        "llm_guardrail_proxy.proxy.app:create_default_app",
        host=settings.listen_host,
        port=settings.listen_port,
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
