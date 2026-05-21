# llm-guardrail-proxy

A lightweight, high-performance Python proxy for intercepting, auditing, and cost-controlling developer prompts before they reach external LLM providers (OpenAI, Anthropic, et al.).

The proxy is built on a strict zero-egress validation principle: every guardrail must be computable locally, with no paid third-party dependency.

## Architectural Phases

| Phase | Title                                                         | Status        |
|------:|---------------------------------------------------------------|:-------------:|
|     1 | Tokenomics & Cost Foundation                                  | Complete      |
|     2 | Async HTTP Reverse Proxy & Middleware Pipeline                | Complete      |
|    3a | Content Guardrails — Secret Detection (regex catalogue)       | Complete      |
|    3b | Content Guardrails — PII Detection (Presidio, BLOCK / REDACT) | Complete      |
|    4a | FinOps Audit Plane — Record schema + sinks (in-memory, JSONL) | Complete      |
|    4b | FinOps — structlog + DuckDB sink + Composite fan-out          | Complete      |
|    4c | FinOps — Read-only `/stats/*` + HTML dashboard                | Complete      |
|    5a | CI/CD — Shift-left CLI (`llm-guardrail-scan`)                 | Complete      |
|    5b | CI/CD — pre-commit hook + reusable GitHub Actions workflow    | **Complete**  |
|   4b' | FinOps — OpenTelemetry traces                                 | Planned       |
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

Open the live dashboard at **http://127.0.0.1:8080/stats/dashboard** for
an auto-refreshing view of the audit ring (verdicts, costs, latency,
findings — every 5 seconds). The page is fully self-contained: no CDN
fonts, no external scripts, no third-party calls.

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

For a real-server validation (boots a uvicorn child, sends live requests,
inspects the JSONL audit ledger), use the Phase 4a smoke script:

```powershell
python scripts/smoke_phase4.py
```

The script terminates the child process automatically and asserts that
the audit plane upholds its non-leakage contract against a real socket.

### Shift-left scanning (`llm-guardrail-scan`)

The same guardrail pipeline is exposed as a one-shot CLI for use in
pre-commit hooks and CI gates. It returns exit `0` on clean input,
`1` on a rejection, and `2` on malformed input.

```powershell
# Scan a prompt file (provider is auto-detected).
llm-guardrail-scan --file prompts/agent.json

# Scan a literal string, JSON output.
llm-guardrail-scan --text "summarise this" --model gpt-4o

# Pipe stdin.
Get-Content prompts/agent.txt | llm-guardrail-scan --model gpt-4o

# Enable tokenomics + PII (PII requires the [pii] extra).
llm-guardrail-scan --file prompts/agent.json --tokens --max-tokens 8000 --pii
```

Defaults are deliberately permissive on tokenomics and PII so commit-
time false positives don't train developers to bypass the hook;
`secret_scan` is the only check enabled out of the box.

### Adopting in a downstream repository

**Pre-commit hook.** Add to `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/Ema322/llm-guardrail-proxy
  rev: v0.5.0
  hooks:
    - id: llm-guardrail-scan
```

**Reusable GitHub Actions workflow.** Add a job that calls the composite
action:

```yaml
jobs:
  guardrail:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Ema322/llm-guardrail-proxy@v0.5.0
        with:
          files: "prompts/**/*.json"
          tokens: "true"
          max-tokens: "8000"
```

The action installs the package from this repository at the pinned ref
and invokes `llm-guardrail-scan` against the matched files. PII can be
enabled with `pii: "true"` — that path additionally downloads the spaCy
English model on the runner.

### Enabling PII detection (optional extra)

PII scanning is opt-in because Presidio + spaCy + the English model add
~80 MB to the install. Enable it with:

```powershell
pip install -e ".[pii]"
python -m spacy download en_core_web_sm
$env:GUARDRAIL_ENABLE_PII_SCANNING = "true"
$env:GUARDRAIL_PII_POLICY = "redact"      # or "block"
python -m llm_guardrail_proxy
```

The PII test modules (`test_pii_*`, `test_proxy_app_phase3b`) auto-skip
when Presidio or the spaCy model is missing, so the base CI stays green
on minimal environments.

## License

Internal portfolio project. Not yet licensed for redistribution.
