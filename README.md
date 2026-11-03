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
  are rejected with `403`; obvious documentation placeholders
  (`AKIAXXXXXXXXXXXXXXXX`, `...EXAMPLE`) are let through. Coverage is measured
  against a labelled corpus ([Detection quality](#detection-quality)).
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
pip install "llm-guardrail-proxy @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.1"
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
       "content": "Why does boto fail? aws_access_key_id = AKIAZ7Q2WXN4LKD3PRV6"}]}'
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
          "span": [40, 60],
          "preview": "AKIA***V6"
        }
      ]
    }
  },
  "request_id": "bdeb8989-12a9-4108-97b9-ff2931e91540"
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
pip install "llm-guardrail-proxy[pii] @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.1"
python -m spacy download en_core_web_sm

pip install "llm-guardrail-proxy[duckdb] @ git+https://github.com/Carlosmarroquin20/llm-guardrail-proxy@v0.5.1"
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
    rev: v0.5.1
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
      - uses: Carlosmarroquin20/llm-guardrail-proxy@v0.5.1
        with:
          files: "prompts/**/*.json"
          tokens: "true"
          max-tokens: "8000"
```

Set `pii: "true"` to also run PII detection; the action then installs the
spaCy model on the runner.

## Detection quality

`evals/secret_detection` builds a labelled corpus from a fixed seed and scores
the secret scanner against it:

- **1,080 positives** — every documented variant of each supported format
  (e.g. AWS `AKIA` and `ASIA` keys; OpenAI legacy, project, service-account
  and admin keys; Slack bot, user and app tokens; five PEM key types),
  embedded in env files, JSON, YAML, URLs, headers, code and prose.
- **760 hard negatives** — strings that share a prefix, alphabet or length
  with a credential: UUIDs, git and SHA-256 hashes, base64 blobs, npm
  integrity hashes, ARNs, Stripe publishable keys and object IDs, public keys
  and certificates, long `sk-proj-...` kebab-case slugs, prefixes mentioned in
  prose.
- **80 placeholders**, reported separately.

```bash
python -m evals.secret_detection.run
```

| | Result |
|---|---:|
| Recall | 100% |
| Precision | 100% |
| Harmless prompts blocked | 0% |
| Placeholders blocked | 0% |

`tests/test_secret_detection_eval.py` fails CI if any of these regress.

Read these numbers for what they are: the corpus is generated from published
formats by the same project that wrote the patterns, so it shows that every
documented shape is covered and that common lookalikes are not flagged. It
does not measure how the scanner does on real leaks in the wild, on secrets
with no distinctive format, or on deliberately obfuscated input.

The first run of this evaluation found real gaps — temporary AWS keys,
GitHub refresh tokens, OpenAI project / service-account / admin keys, Slack
app tokens, Stripe restricted keys and some keys ending in `-` were missed
(75% recall overall) — and 64% of placeholders were blocked. Those are fixed and now
covered by unit tests.

## Performance

`benchmarks/latency.py` starts the proxy and a mock upstream that answers
instantly, then sends identical requests directly and through the proxy. The
proxy runs with its shipped defaults (secret scan, tokenomics, in-memory and
log audit sinks) in a single uvicorn worker.

Added latency at concurrency 1, 1,000 requests per row:

| Prompt | Added p50 | Added p99 |
|---|---:|---:|
| ~60 tokens | +3.8 ms | +5.2 ms |
| ~1,000 tokens | +5.2 ms | +7.1 ms |
| ~6,000 tokens | +10.5 ms | +10.9 ms |

Throughput through one worker was about 140–170 req/s for short and medium
prompts. For 6,000-token prompts it was about 80 req/s at concurrency 1 and
160 req/s at concurrency 16, because token counting runs in worker threads
and overlaps across requests.

Measured on Windows 11, Python 3.10, 12 logical CPUs, with client, proxy and
mock on the same machine. Treat these as orders of magnitude; the
[Benchmark workflow](.github/workflows/benchmark.yml) reruns them on a Linux
runner. Against a real provider, where a completion takes hundreds of
milliseconds, the added time is a small fraction of the call.

```bash
python -m benchmarks.latency            # add --pii to include PII scanning
```

Profiling with this benchmark led to two changes:

- Secret patterns now start with their literal prefix and check the
  preceding character with a look-behind instead of a leading `\b`. Python's
  regex engine can only skip ahead to candidate positions when a pattern
  starts with a literal, so the full catalogue went from 4.9 ms to 0.3 ms on a
  31 KB prompt.
- PII scanning moved off the event loop. With PII enabled, a 6,000-token
  prompt spends about 1.3 s in spaCy; before the change that blocked every
  other request, health checks included, for the whole time.

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
  high-entropy strings, passwords or AWS secret access keys, which have no
  distinctive prefix.
- PII scanning is expensive: about +20 ms on a short prompt, +210 ms at
  1,000 tokens and +1.3 s at 6,000 tokens in the benchmark above. It runs in
  a worker thread so other requests keep moving, but each scanned request
  pays that cost.
- The in-memory audit ring behind `/stats` is per process, so running several
  uvicorn workers gives each its own view. Use the JSONL or DuckDB sink for a
  combined record.
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
evals/           Labelled corpus and scoring for the secret scanner
benchmarks/      Latency benchmark and mock upstream
scripts/         Smoke tests against a real uvicorn process
```

## Development

```bash
git clone https://github.com/Carlosmarroquin20/llm-guardrail-proxy
cd llm-guardrail-proxy
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"        # add ,duckdb and/or ,pii for the optional extras
pytest
ruff check src tests scripts evals benchmarks
mypy                           # strict mode, configured in pyproject.toml
```

The suite has 280 tests and runs without network access; PII and DuckDB tests
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
