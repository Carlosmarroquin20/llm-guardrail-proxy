"""Top-level package for the llm-guardrail-proxy project.

Public re-exports are intentionally minimal at this stage: keeping the import
surface narrow prevents accidental coupling between proxy middleware (Phase 2+)
and the pure cost-evaluation domain delivered in Phase 1.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("llm-guardrail-proxy")
except PackageNotFoundError:  # editable install before metadata is materialised
    __version__ = "0.1.0"

__all__ = ["__version__"]
