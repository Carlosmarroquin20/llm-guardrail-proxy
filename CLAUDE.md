# CLAUDE.md

Operational brief for Claude Code sessions in this repository. Keep it tight —
this file is loaded into every conversation. Update when invariants change.

## What this project is

`llm-guardrail-proxy`: a local, zero-egress reverse proxy that intercepts
developer prompts before they reach external LLM APIs (OpenAI, Anthropic).
Built in 5 sequential phases. Every guardrail must be computable locally;
**no paid third-party service is acceptable in the runtime path.**

## Phase status

| Phase | Title                                                  | Status     |
|------:|--------------------------------------------------------|:----------:|
|     1 | Tokenomics & Cost Foundation                           | Complete   |
|     2 | Async HTTP Reverse Proxy & Middleware Pipeline         | Complete   |
|    3a | Content Guardrails — Secret Detection (regex)          | Complete   |
|    3b | Content Guardrails — PII Detection (Presidio)          | Complete   |
|     4 | FinOps Observability & Audit Plane                     | Pending    |
|     5 | CI/CD Distribution & Shift-Left Integration            | Pending    |

Do **not** generate code for a future phase until the user explicitly asks.

## Layout

```
src/llm_guardrail_proxy/
  core/                   Pure-Python domain logic (no I/O). Phase 1.
    exceptions.py         GuardrailError hierarchy.
    pricing.py            Decimal price matrix, MappingProxyType-protected.
    models.py             Frozen Pydantic value objects.
    tokenomics.py         TokenomicsService (sync, CPU-bound).
  config/thresholds.py    Conservative default ThresholdPolicy.
  proxy/                  Async ASGI surface. Phase 2.
    settings.py           pydantic-settings, GUARDRAIL_* env prefix.
    envelope.py           ProxyRequest, ParsedPrompt, Continue|Reject|Mutate.
    providers.py          OpenAI Chat + Anthropic adapters (parse + redact).
    middleware.py         Middleware Protocol (structural).
    middlewares/          Concrete pipeline links.
      secret_scan.py        Phase 3a: regex-based credential detector.
      pii_scan.py           Phase 3b: Presidio PII detector (BLOCK/REDACT).
      tokenomics.py         Phase 2: Phase 1 service as a pipeline link.
    pipeline.py           MiddlewarePipeline orchestrator + Mutate handling.
    circuit_breaker.py    CLOSED→OPEN→HALF_OPEN async breaker.
    forwarder.py          httpx streaming relay + hop-by-hop filter.
    app.py                FastAPI factory (build_app + create_default_app).
    scanning/             Content-scanning primitives.
      findings.py           Severity + ScanFinding value objects.
      patterns.py           Curated SecretPattern catalogue (Phase 3a).
      secrets.py            SecretScanner driver (Phase 3a).
      pii.py                PiiScanner (Phase 3b, lazy Presidio import).
  __main__.py             uvicorn launcher.
tests/                    pytest suite, all in-process (no sockets).
```

## Architectural invariants

- **Money is always `Decimal`** — never `float`. Verified by tests.
- **Phase 1 core is sync.** Async middleware offloads via
  `anyio.to_thread.run_sync` because tiktoken is CPU-bound.
- **Frozen value objects.** `ProxyRequest`, `ParsedPrompt`, `EvaluationResult`,
  `CostEstimate`, `ThresholdPolicy` are immutable. Phase 4's audit plane
  depends on this.
- **`Continue | Reject | Mutate` sum-type** is the middleware outcome
  contract. `Mutate` carries `replacements: tuple[tuple[str, str], ...]`;
  the pipeline applies them via the resolved provider adapter's `redact`
  method and threads the rewritten envelope forward. Do not change these
  variants.
- **Provider adapters are the only place** wire-format quirks live. Adding
  a provider = enum entry + adapter + registry line. Nothing else.
- **Path-based dispatch, no wildcards.** Unknown paths → 404 from FastAPI
  itself; the proxy refuses to forward what it cannot inspect.
- **Build-time DI.** `build_app(settings, pipeline, forwarder)` accepts every
  collaborator. Tests inject `httpx.MockTransport`; production uses
  `create_default_app`. Never reach for module-level globals.

## Conventions in this repo

- **Python 3.10+** (was 3.11 originally; backported because the dev box has
  3.10). `StrEnum` is therefore replaced by `class X(str, Enum)`. Keep that
  pattern for any new enums.
- **Type hints everywhere** (PEP 484). `from __future__ import annotations`
  at the top of every module.
- **Docstrings explain WHY, not WHAT.** No generic AI commentary. No emojis.
  No TODOs in code — open an issue or do it now.
- **Custom exceptions per layer.** `GuardrailError` (core) and `ProxyError`
  (proxy) hierarchies are deliberately disjoint; do not collapse them.
- **No `print()`.** Use the `llm_guardrail_proxy` logger.

## Running things

Virtual environment lives at **`.venv/`** (already created, Python 3.10.11).

```powershell
.\.venv\Scripts\python.exe -m pytest             # full suite, ~2s
.\.venv\Scripts\python.exe -m llm_guardrail_proxy   # launch on 127.0.0.1:8080
```

VS Code interpreter must point at `.venv\Scripts\python.exe` or every import
will look "missing" in the editor (deps are installed inside the venv only).

Last verified runs:
- **Without Presidio:** 118 passed, 3 skipped (~2 s).
- **With `[pii]` extra + spaCy model:** 139 passed (~3 min — Presidio cold
  start dominates).

## Gotchas worth remembering

- **`httpx.MockTransport` + `httpx.Response(200, json=...)`** marks the stream
  as consumed during construction. `forwarder._iter_body` handles this by
  falling back to `response.content` when `is_stream_consumed` is True. Do
  not "simplify" that helper away — it is load-bearing for the test suite.
- **Hop-by-hop headers** are stripped both inbound and outbound by
  `_filter_headers` in `forwarder.py`. The `host` and `content-length`
  headers must always be filtered; without that the upstream sees the proxy
  hostname and rejects the request.
- **Circuit breaker uses an injectable clock.** Tests pass a manual clock;
  never call `time.sleep` in tests. The breaker's failure counter only
  resets on a successful call, not on time elapsed.
- **`ThresholdPolicy` rejects all-None at construction.** A policy that
  enforces nothing is almost always a bug; the test
  `test_empty_policy_is_rejected` will fail if that guard is removed.
- **Pricing fallback is conservative on purpose.** Unknown models get the
  highest listed input rate — never silently under-price. Don't "improve"
  it to a median or average without discussion.
- **Secret-scan ordering matters.** `SecretScanMiddleware` runs before
  `TokenomicsMiddleware` in `app.py`. Don't flip the order: a leaked secret
  in an oversized prompt must surface the security verdict, not the FinOps
  one.
- **`ScanFinding.preview` never carries the full secret.** `_redact` keeps
  a 4-char prefix and 2-char suffix only. Auditing the full match would
  re-leak it through the diagnostic plane — the whole point of the scanner.
- **PII scanning is opt-in.** `enable_pii_scanning` defaults to False
  because Presidio + spaCy + the en_core_web_sm model add ~80 MB. Test
  modules `test_pii_*` and `test_proxy_app_phase3b` `pytest.importorskip`
  on Presidio AND `spacy.load("en_core_web_sm")` so the base suite stays
  green on minimal environments.
- **`PiiScanner` lazy-imports Presidio inside `__init__`.** This is
  deliberate: importing the module must never fail when Presidio is
  absent — only instantiating the scanner does, via `MissingPiiBackend`.
- **Pipeline order is secret → pii → tokenomics.** Security verdicts
  outrank FinOps; among security verdicts, secrets outrank PII because
  leaked credentials cannot be safely redacted in place.
- **Redaction uses `str.replace` semantics:** every occurrence is rewritten.
  Over-redaction is benign; under-redaction is a leak. Do not "optimise" to
  replace only the matched span.

## What NOT to do

- Don't add new runtime dependencies without bumping `requirements.txt`
  *and* `pyproject.toml.[project].dependencies` together.
- Don't introduce a new middleware by editing the pipeline orchestrator;
  add a class implementing `Middleware` and register it in `app.py`.
- Don't add streaming SSE-specific code in the forwarder. It is already
  format-agnostic; SSE Just Works because we copy the upstream headers.
- Don't import from `proxy/` inside `core/`. The dependency direction is
  one-way (`proxy → core`).
- Don't replace `Decimal` with `float` anywhere in pricing math.
