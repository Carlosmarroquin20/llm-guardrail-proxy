# llm-guardrail-proxy

[![CI](https://github.com/Carlosmarroquin20/llm-guardrail-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Carlosmarroquin20/llm-guardrail-proxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)

A local reverse proxy that sits between your code and the OpenAI / Anthropic
APIs and inspects every prompt before it leaves your machine. It blocks
leaked credentials, blocks or redacts PII, and enforces token and cost
budgets — and records what it decided.

Every check runs locally. There is no hosted service, no API key for the
proxy itself, and no third-party call in the request path.

![Live stats dashboard](docs/images/dashboard.jpg)

## Features

- **Secret detection** — regex catalogue for AWS, GitHub, OpenAI, Anthropic,
  Slack, Stripe and Google keys, JWTs and PEM private keys. Matching prompts
  are rejected with `403`.
- **PII detection** (optional) — [Presidio](https://github.com/microsoft/presidio)
  based, with a `block` policy (reject) or a `redact` policy (rewrite the
  prompt and forward it).
- **Token and cost budgets** — counts prompt tokens with `tiktoken` and
  rejects prompts over a token or USD limit with `413`. All money math uses
  `Decimal`.
- **Audit log** — one record per request (allowed, rejected or upstream
  failure) written to any combination of an in-memory ring, JSONL, DuckDB and
  structured logs. Records hold redacted previews only, never the prompt or
  the secret itself.
- **Stats API and dashboard** — `/stats/summary`, `/stats/recent` and a
  self-contained HTML dashboard at `/stats/dashboard`.
- **Shift-left scanning** — the same checks as a CLI (`llm-guardrail-scan`),
  a pre-commit hook and a GitHub Action, so prompt files with secrets are
  caught before they are committed.
- **Transparent forwarding** — streamed responses (including SSE), request
  IDs, and a circuit breaker in front of upstream calls.

## How it works

```mermaid
flowchart TD
    C[Client / SDK] --> P[Provider adapter: parse prompt]
    P --> S[Secret scan]
    S -->|clean| I[PII scan, optional]
    I -->|clean or redacted| T[Token and cost check]
    T -->|within budget| F[Forwarder + circuit breaker]
    F --> U[(OpenAI / Anthropic)]
    S -.->|403| R[Rejection response]
    I -.->|403| R
    T -.->|413| R
    F --> A[Audit sinks: memory, JSONL, DuckDB, logs]
    R --> A
    A --> D[Stats API and dashboard]
```

Each middleware returns one of three outcomes: `Continue`, `Reject` (stop and
answer the client) or `Mutate` (rewrite the prompt, e.g. PII redaction, and
continue). The order is fixed — secrets, then PII, then cost — so a prompt
that both leaks a key and exceeds the budget is reported as a security
problem, not a billing one.

Requests to paths the proxy does not understand get a `404`; it never
forwards traffic it cannot inspect.

## Quick start

Requires Python 3.10+.

```bash
pip install "llm-guardrail-proxy @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.0"
llm-guardrail-proxy          # listens on http://127.0.0.1:8080
```

Point your SDK at the proxy instead of the provider. Your provider API key is
passed through unchanged.

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1")

from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8080")
```

Or set `OPENAI_BASE_URL=http://127.0.0.1:8080/v1` /
`ANTHROPIC_BASE_URL=http://127.0.0.1:8080` and leave the code alone.

### What a blocked request looks like

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user",
       "content": "Why does boto fail? aws_access_key_id = AKIAIOSFODNN7EXAMPLE"}]}'
```

```json
{
  "error": "secret_exposure_detected",
  "detail": "Prompt contains 1 secret-like value(s); refusing to forward.",
  "middleware": "secret_scan",
  "annotations": {
    "secret_scan": {
      "finding_count": 1,
      "findings": [
        {
          "kind": "aws_access_key_id",
          "label": "AWS Access Key ID",
          "severity": "high",
          "span": [53, 73],
          "preview": "AKIA***LE"
        }
      ]
    }
  },
  "request_id": "7047afd8-fe2d-4bf0-9494-d00d995f9824"
}
```

The request never reaches the provider. The same request ID is returned in
the `X-Request-Id` header and written to the audit log.

## Configuration

Settings are read from environment variables (or a `.env` file), grouped by
concern as `GUARDRAIL_<GROUP>__<FIELD>`. The most common ones:

| Variable | Default | Purpose |
|---|---|---|
| `GUARDRAIL_NETWORK__LISTEN_PORT` | `8080` | Port to listen on (host defaults to `127.0.0.1`). |
| `GUARDRAIL_TOKENOMICS__MAX_PROMPT_TOKENS` | `8000` | Reject prompts above this many tokens. |
| `GUARDRAIL_TOKENOMICS__MAX_PROMPT_COST_USD` | `0.05` | Reject prompts whose estimated input cost exceeds this. |
| `GUARDRAIL_SCANNING__ENABLE_SECRETS` | `true` | Secret detection. |
| `GUARDRAIL_SCANNING__ENABLE_PII` | `false` | PII detection (needs the `[pii]` extra). |
| `GUARDRAIL_SCANNING__PII_POLICY` | `block` | `block` or `redact`. |
| `GUARDRAIL_AUDIT__JSONL_PATH` | unset | Append audit records to a JSONL file. |
| `GUARDRAIL_AUDIT__DUCKDB_PATH` | unset | Write audit records to DuckDB (needs the `[duckdb]` extra). |
| `GUARDRAIL_STATS__ENABLE_ENDPOINT` | `true` | Serve `/stats/*` and the dashboard. |

See [`.env.example`](.env.example) for the full list.

### Optional extras

```bash
pip install "llm-guardrail-proxy[pii] @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.0"
python -m spacy download en_core_web_sm

pip install "llm-guardrail-proxy[duckdb] @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.0"
```

PII support is off by default because Presidio, spaCy and the English model
add roughly 80 MB.

## Scanning prompt files before commit

`llm-guardrail-scan` runs the same checks against prompt files (OpenAI or
Anthropic request bodies as JSON) without starting the server.

```console
$ llm-guardrail-scan prompts/summary.json prompts/debug.json --format text
prompts/summary.json: PASS
prompts/debug.json: FAIL (secret_scan: secret_exposure_detected)
    - OpenAI API Key severity=high preview=sk-p***3i
summary: scanned=2 allowed=1 rejected=1
```

Exit codes: `0` clean, `1` rejected, `2` invalid input. Only secret scanning
is on by default; add `--tokens`, `--max-tokens N`, `--max-cost USD` or
`--pii` to enable the other checks. The CLI ignores `GUARDRAIL_*` variables
so a hook behaves the same on every machine.

**pre-commit**

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Carlosmarroquin20/llm-guardrail-proxy
    rev: v0.5.0
    hooks:
      - id: llm-guardrail-scan
```

**GitHub Actions**

```yaml
jobs:
  guardrail:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: Carlosmarroquin20/llm-guardrail-proxy@v0.5.0
        with:
          files: "prompts/**/*.json"
          tokens: "true"
          max-tokens: "8000"
```

Set `pii: "true"` to also run PII detection; the action then installs the
spaCy model on the runner.

## Design notes

- **Fail closed on unknown input.** Unknown paths return `404` and malformed
  bodies return `400` instead of being passed through.
- **Unknown models are priced at the highest known rate**, so a budget check
  can over-estimate but never silently under-estimate.
- **Redaction replaces every occurrence** of a detected value, not only the
  matched span. Over-redaction is harmless; a missed copy is a leak.
- **Observability failures never fail a request.** If one audit sink errors,
  the others still write and the client still gets its response.
- **Audit is recorded at the transport layer**, not in a middleware, because
  it needs the upstream status code and total latency.
- **Everything is injected at build time.** `build_app(settings, pipeline,
  forwarder)` takes every collaborator, which is how the test suite runs the
  full proxy in-process against `httpx.MockTransport` without opening a
  socket.

## Limitations

- The price table covers OpenAI models. Other models, including Claude, are
  costed at the fallback (highest) rate and their tokens are counted with a
  `tiktoken` encoding, so both numbers are approximations.
- Cost checks estimate the **prompt** only; completion tokens are not known
  before the request is sent.
- Secret detection matches known key formats. It does not flag arbitrary
  high-entropy strings.
- Responses are relayed as-is; only outgoing prompts are inspected.
- `/stats` reads the in-memory ring, which resets on restart. Use the JSONL or
  DuckDB sink for history.
- The proxy has no authentication of its own. It binds to `127.0.0.1` by
  default; if you expose it, disable `/stats` or put it behind auth, since it
  shows finding previews and cost data.

## Project layout

```
src/llm_guardrail_proxy/
  core/          Pricing and token counting (pure Python, no I/O)
  config/        Default threshold policy
  proxy/         FastAPI app, providers, pipeline, forwarder, circuit breaker
    middlewares/ Secret, PII and tokenomics checks
    scanning/    Secret pattern catalogue and Presidio wrapper
    audit/       Audit record schema and sinks
    stats/       /stats endpoints and dashboard
  cli/           llm-guardrail-scan
tests/           Unit and in-process end-to-end tests
scripts/         Smoke tests against a real uvicorn process
```

## Development

```bash
git clone https://github.com/Carlosmarroquin20/llm-guardrail-proxy
cd llm-guardrail-proxy
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"        # add ,duckdb and/or ,pii for the optional extras
pytest
ruff check src tests scripts
mypy                           # strict mode, configured in pyproject.toml
```

The suite has 241 tests and runs without network access; PII and DuckDB tests
are skipped when those extras are not installed. CI runs ruff and mypy, the
tests on Python 3.10, 3.11 and 3.12, a job with the PII extra, and a job that
runs the GitHub Action against this repository.

The scripts in `scripts/` start a real server and check the audit log, stats
endpoints, dashboard and CLI end to end, e.g. `python scripts/smoke_phase4.py`.

## Roadmap

- OpenTelemetry traces for the request pipeline.
- Pricing data for Anthropic models.

## License

[MIT](LICENSE)
