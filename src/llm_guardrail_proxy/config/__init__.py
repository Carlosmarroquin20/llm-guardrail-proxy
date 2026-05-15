"""Static configuration defaults.

Kept separate from ``core`` so that Phase 2's environment-driven configuration
loader can replace these defaults without touching the domain layer.
"""

from llm_guardrail_proxy.config.thresholds import DEFAULT_POLICY

__all__ = ["DEFAULT_POLICY"]
