# llm-guardrail-proxy

A lightweight, high-performance Python proxy for intercepting, auditing, and cost-controlling developer prompts before they reach external LLM providers (OpenAI, Anthropic, et al.).

The proxy is built on a strict zero-egress validation principle: every guardrail must be computable locally, with no paid third-party dependency.

## Architectural Phases

| Phase | Title | Status |
|------:|-------|:------:|
| 1 | Tokenomics & Cost Foundation                                  | **Active** |
| 2 | Async HTTP Reverse Proxy & Middleware Pipeline                | Planned    |
| 3 | Content Guardrails — PII & Secret Detection (Presidio)        | Planned    |
| 4 | FinOps Observability & Audit Plane (structlog / OTel / DuckDB)| Planned    |
| 5 | CI/CD Distribution & Shift-Left Integration (pre-commit / GH) | Planned    |

## Phase 1 — Tokenomics & Cost Foundation

Phase 1 delivers the deterministic cost-evaluation primitive consumed by every later phase:

- Network-free token counting via `tiktoken` with safe encoding fallback.
- Configurable per-model price matrix expressed in `Decimal` to avoid float drift.
- Threshold engine producing a structured evaluation verdict suitable for middleware dispatch.

## Layout

```
src/llm_guardrail_proxy/   Production package
  core/                    Pure-Python domain logic (no I/O)
  config/                  Static, environment-overridable defaults
tests/                     Pytest suite (unit-scope)
```

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

## License

Internal portfolio project. Not yet licensed for redistribution.
