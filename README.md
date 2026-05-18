# llm-guardrail-proxy

A lightweight, high-performance Python proxy for intercepting, auditing, and cost-controlling developer prompts before they reach external LLM providers (OpenAI, Anthropic, et al.).

The proxy is built on a strict zero-egress validation principle: every guardrail must be computable locally, with no paid third-party dependency.

## Architectural Phases

| Phase | Title                                                         | Status        |
|------:|---------------------------------------------------------------|:-------------:|
|     1 | Tokenomics & Cost Foundation                                  | Complete      |
|     2 | Async HTTP Reverse Proxy & Middleware Pipeline                | Complete      |
|    3a | Content Guardrails — Secret Detection (regex catalogue)       | **Active**    |
|    3b | Content Guardrails — PII Detection (Presidio)                 | Planned       |
|     4 | FinOps Observability & Audit Plane (structlog / OTel / DuckDB)| Planned       |
|     5 | CI/CD Distribution & Shift-Left Integration (pre-commit / GH) | Planned       |

## Phase 2 — Async Reverse Proxy

Phase 2 puts the Phase 1 evaluator behind an ASGI reverse proxy:

- **FastAPI + httpx** request pipeline with streamed upstream responses.
- **Typed middleware chain** — middlewares return `Continue` or `Reject` so policy enforcement points compose cleanly.
- **Async circuit breaker** in front of every upstream call.
- **Provider adapters** for `POST /v1/chat/completions` and `POST /v1/messages` — additional providers plug in via a single dispatch table.
- **Environment-driven configuration** via `pydantic-settings` (`GUARDRAIL_*` namespace; see `.env.example`).

### Running the proxy

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
python -m llm_guardrail_proxy
```

Then point any OpenAI-compatible SDK at the proxy:

```powershell
$env:OPENAI_BASE_URL = "http://127.0.0.1:8080/v1"
```

### Layout

```
src/llm_guardrail_proxy/
  core/                     Pure-Python domain logic (Phase 1)
  config/                   Static, environment-overridable defaults
  proxy/                    Async ASGI surface (Phase 2)
    middlewares/            Concrete pipeline links
tests/                      Pytest suite (unit + ASGI end-to-end)
```

### Development

```powershell
pip install -r requirements-dev.txt
pytest
```

The end-to-end tests drive the ASGI app in-process via `httpx.ASGITransport`
and intercept upstream traffic with `httpx.MockTransport`; no socket is
opened during the suite.

## License

Internal portfolio project. Not yet licensed for redistribution.
