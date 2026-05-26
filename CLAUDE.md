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
|    4a | FinOps Audit — Record schema + AuditSink (mem, JSONL)  | Complete   |
|    4b | FinOps — structlog + DuckDB sink + Composite fan-out   | Complete   |
|    4c | FinOps — /stats/* JSON + HTML dashboard                | Complete   |
|    5a | CI/CD — Shift-left CLI (llm-guardrail-scan)            | Complete   |
|    5b | CI/CD — pre-commit + reusable GH Actions workflow      | Complete   |
|   4b' | FinOps — OpenTelemetry traces                          | Pending    |
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
    settings.py           Nested pydantic-settings groups (network,
                          breaker, tokenomics, scanning, audit, stats,
                          logging) — GUARDRAIL_<group>__<field> env vars.
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
    handler.py            Per-request lifecycle: parse → pipeline → audit
                          → forward. Single audit emission site.
    app.py                FastAPI factory (build_app + create_default_app).
    scanning/             Content-scanning primitives.
      findings.py           Severity + ScanFinding value objects.
      patterns.py           Curated SecretPattern catalogue (Phase 3a).
      secrets.py            SecretScanner driver (Phase 3a).
      pii.py                PiiScanner (Phase 3b, lazy Presidio import).
    audit/                FinOps audit plane (Phase 4a-b).
      records.py            AuditRecord schema + build_audit_record.
      sinks.py              AuditSink Protocol + Null/InMemory/Jsonl impls.
      composite.py          Fan-out sink with isolated failure semantics.
      logging_sink.py       structlog sink + configure_logging helper.
      duckdb_sink.py        DuckDB sink (lazy import, [duckdb] extra).
    stats/                  Read-side query surface (Phase 4c).
      repository.py         StatsRepository Protocol + summarise aggregator.
      router.py             FastAPI router for /stats/summary + /stats/recent.
      dashboard.py          Embedded self-contained HTML at /stats/dashboard.
  cli/                    Shift-left CLI (Phase 5a-b).
    scan.py                 main(argv) + cli() entry, batch & single modes.
    formatters.py           JSON / text renderers, single and batch shapes.
    __main__.py             Enables ``python -m llm_guardrail_proxy.cli``.
.pre-commit-hooks.yaml    Phase 5b: hook metadata for downstream repos.
action.yml                Phase 5b: composite GitHub Action.
.github/workflows/ci.yml  Phase 5b: internal CI (matrix + PII + dogfood).
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
- **Without Presidio:** 217 passed, 3 skipped (~5 s).
- **With `[pii]` extra + spaCy model:** 238 passed (~25 s warm).
- **Smoke scripts:**
  - `scripts/smoke_phase4.py` — single sink JSONL end-to-end.
  - `scripts/smoke_phase4b.py` — composite (JSONL + DuckDB + structlog)
    fan-out + cross-sink request_id consistency + no-re-leakage.
  - `scripts/smoke_phase4c.py` — /stats/summary and /stats/recent against
    a real uvicorn process with a synthetic rejection workload.
  - `scripts/smoke_phase5.py` — CLI subprocess against four fixtures
    (clean / leak / tokenomics / malformed) — verifies exit-code
    contract and the no-re-leakage invariant on stdout/stderr.
  - `scripts/smoke_phase5b.py` — pre-commit `run --all-files` against a
    synthetic consumer repo with two staged fixtures (clean + leaking);
    validates the runtime contract that `.pre-commit-hooks.yaml`
    publishes to downstream adopters.
  - `scripts/smoke_dashboard.py` — HTML dashboard at /stats/dashboard
    against a real uvicorn process: anchors present, no external
    assets, JSON polling endpoints reachable.

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
- **Audit is transport-layer, not middleware-layer.** Recording happens
  inside `handle_proxied_request` in `proxy/handler.py`, not in a
  middleware, because audit needs the `upstream_status_code` and total
  latency that no single middleware can observe. Don't try to refactor
  it into a "logging middleware" — the pipeline contract has no terminal
  hook.
- **`AuditRecord` never carries raw prompts or full secret/PII values.**
  Only redacted previews emitted by the scanners. If you add a new field
  that could plausibly carry sensitive content, the audit plane has to be
  reviewed end-to-end before merging.
- **Audit is recorded on every terminating path:** rejection, upstream
  failure, success. Exactly one record per request. Pre-parse failures
  (404 / 400 malformed body) deliberately skip audit — no model, no
  provider, nothing meaningful to write.
- **`X-Request-Id` is honoured if inbound and well-formed.** Malformed
  values are silently replaced with a fresh UUID — the proxy never 400s
  on a bad header. The header is always echoed on the response.
- **Latency captured is decision-plus-headers, not body-stream.** FinOps
  cares about cost (already known at decision time) and verdict; full-body
  latency would require holding the response open. Documented in `app.py`.
- **`PipelineDecision.final_request` is the source of truth for forwarding
  and for `mutations_applied`.** When `final_request is request`, no
  middleware mutated the envelope; record this as `mutations_applied=False`.
- **`CompositeAuditSink` isolates per-sink failures.** A failing JSONL
  write must never starve the DuckDB sink, and vice versa. Do not "fix"
  the silent catch by re-raising — the alternative is a 5xx for the
  client because of an observability failure.
- **The in-memory ring is always part of the composite when auditing is
  enabled.** Phase 4c's `/stats` endpoint reads it; removing it from
  `_build_audit_sink` breaks that future contract.
- **`configure_logging` is idempotent.** Calling it twice does not stack
  processors. Tests that need different formats call it again and trust
  the replace semantics; do not introduce a `_CONFIGURED` early-return
  guard or those tests start observing stale config.
- **DuckDB table name is interpolated into DDL, not parameterised.** The
  validator (`isalnum() or '_'`) is therefore load-bearing for safety. Do
  not relax it.
- **DuckDB sink is opt-in via the `[duckdb]` extra.** Lazy-import in
  `__init__` raises `MissingAuditBackend` when the wheel is absent.
  `test_audit_duckdb_sink` uses `pytest.importorskip`.
- **Stats router reads via `StatsRepository`, not via the audit sink.**
  `_build_audit_sink` returns `(composite, memory_sink)`; the memory
  sink is passed as both the write target (inside the composite) and
  the read target (as `stats_repository`). Do not collapse those into
  one parameter — Phase 4d may swap in a DuckDB-backed read repository
  while keeping the same write composite.
- **`/stats/*` is default-on because the proxy binds to localhost.**
  Operators exposing the proxy externally must set
  `enable_stats_endpoint=false` (or front the route with auth) — the
  endpoint surfaces findings previews and cost data.
- **`StatsSummary.total_estimated_cost_usd` is `Decimal`.** It
  serialises to a JSON string by Pydantic default. Tests assert against
  the string form because `float(...)` round-trip would defeat the
  no-float-drift contract.
- **The dashboard HTML loads zero external assets.** The proxy is a
  zero-egress tool; an operator-facing page that reaches a CDN would
  contradict that. `test_dashboard_loads_no_external_assets` pins the
  invariant by failing on any `http(s)://` URL in the markup.
- **Dashboard HTML is embedded as a Python string constant, not a
  bundled asset.** Avoids `package_data` / `MANIFEST.in` configuration
  drift in the wheel; the markup is always shipped with the code.
- **Findings serialise via `ScanFinding.as_dict()` — single source of
  truth.** Adding a field to `ScanFinding` updates the audit shape
  everywhere automatically. Do not re-introduce per-middleware
  `_serialise` helpers; the duplication they create is exactly what
  this method exists to prevent.
- **Settings are nested by concern, not flat.** Access is
  `settings.audit.jsonl_path`, not `settings.audit_jsonl_path`. Env
  vars use the double-underscore delimiter:
  `GUARDRAIL_AUDIT__JSONL_PATH`. Tests construct via dict-init:
  `ProxySettings(network={"openai_base_url": "..."})`.
- **`proxy/__init__.py` re-exports the narrow public surface only**
  (factories, settings, pipeline contract, middleware-authoring
  envelope types). Internal machinery — audit sinks, stats
  aggregators, finding records, parsed prompts — is *not* re-exported
  there; import from the owning sub-module
  (`proxy.audit`, `proxy.stats`, `proxy.envelope`). The
  `__init__.py` files of those sub-modules ARE load-bearing — 42
  import sites in the codebase reference them, so do not prune them
  the same way.
- **The CLI ignores `GUARDRAIL_*` environment variables on purpose.**
  Flags are the only configuration surface — a pre-commit hook whose
  behaviour drifts with the developer's shell becomes unreproducible
  and gets bypassed. Server settings stay server-side.
- **CLI defaults differ from server defaults.** Only `secret_scan`
  runs by default; `--tokens` and `--pii` are opt-in. Commit-time
  false positives train developers to add `--no-verify`; precision
  beats recall for shift-left.
- **CLI exit-code contract: `0` clean, `1` rejected, `2` input error.**
  Constants live in `cli/scan.py`. Pre-commit hooks and CI gates pivot
  on these — do not introduce new codes without bumping the major.
- **Two console scripts, two purposes.** `llm-guardrail-proxy` launches
  uvicorn (server). `llm-guardrail-scan` runs the one-shot scan
  (shift-left). Do not unify them — the operational contexts diverge.
- **`python -m llm_guardrail_proxy.cli` is the portable entry point.**
  The console script may not be on PATH right after an editable
  install; tests and smoke scripts use the `-m` form for that reason.
- **CLI batch-mode shape is determined by *invocation*, not result count.**
  Positional file args → wrapped `{"results": [...], "summary": {...}}`
  output, even with a single file. `--file` / `--text` / stdin → flat
  output. Flipping schema based on count would force pre-commit
  consumers to special-case the 1-file boundary.
- **`.pre-commit-hooks.yaml` and `action.yml` shapes are pinned by
  `test_phase5b_metadata.py`.** A change to the `id`, `entry`,
  `language`, `pass_filenames`, or composite step order breaks every
  downstream adopter — the tests guard the contract.
- **Phase 5b smoke uses `repo: local`, not `try-repo`.** `try-repo`
  clones the repository and requires the hook YAML to be in HEAD; the
  smoke needs to run against the *working directory*. The local-mode
  config exercises the same runtime contract (entry, language,
  pass_filenames) and is faster.
- **Windows path quirk:** YAML stripped backslashes from
  `D:\CODE\...` paths silently. The smoke converts via
  `Path.as_posix()` before writing the config; do not regress this.

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
