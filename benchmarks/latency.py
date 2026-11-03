"""Measure the latency the proxy adds on top of a direct upstream call.

Starts two real processes on loopback — a mock upstream that answers
instantly (``benchmarks/mock_upstream.py``) and the proxy, configured as it
ships (secret scan + tokenomics, in-memory and structlog audit sinks) and
pointed at the mock — then sends the same requests to each and compares.

Usage::

    python -m benchmarks.latency                    # default run
    python -m benchmarks.latency --requests 2000    # tighter percentiles
    python -m benchmarks.latency --pii              # also enable PII (needs [pii])
    python -m benchmarks.latency --json out.json

Caveats worth stating next to any number this prints:

* Client, proxy and mock share one machine, so they compete for CPU; at
  higher concurrency the client's own overhead shows up in both columns.
* The proxy runs as a single uvicorn worker.
* The mock replies in microseconds. Against a real provider, where a call
  takes hundreds of milliseconds, the same absolute overhead is a far
  smaller share.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import tiktoken

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "var" / "bench"
MOCK_PORT = 9911
PROXY_PORT = 9912

_SENTENCES = (
    "Summarise the trade-offs between optimistic and pessimistic locking.",
    "The service retries idempotent requests with exponential backoff and jitter.",
    "Explain why the cache invalidation runs after the database commit, not before.",
    "List three ways to reduce cold-start latency for a containerised Python app.",
    "Each tenant has its own schema, and migrations run per tenant in parallel.",
)


def _prompt(target_tokens: int) -> str:
    enc = tiktoken.get_encoding("o200k_base")
    parts: list[str] = []
    i = 0
    while len(enc.encode(" ".join(parts))) < target_tokens:
        parts.append(_SENTENCES[i % len(_SENTENCES)])
        i += 1
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Result:
    prompt: str
    prompt_tokens: int
    concurrency: int
    target: str
    requests: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    rps: float


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


async def _run_load(
    base_url: str, body: dict[str, object], total: int, concurrency: int, warmup: int
) -> tuple[list[float], float]:
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=30) as client:
        for _ in range(warmup):
            (await client.post("/v1/chat/completions", json=body)).raise_for_status()

        latencies: list[float] = []
        remaining = total

        async def worker() -> None:
            nonlocal remaining
            while remaining > 0:
                remaining -= 1
                start = time.perf_counter()
                response = await client.post("/v1/chat/completions", json=body)
                latencies.append((time.perf_counter() - start) * 1_000)
                response.raise_for_status()

        started = time.perf_counter()
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
    return latencies, elapsed


def _wait_healthy(url: str, deadline_s: float = 20.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not become healthy within {deadline_s}s")


@contextmanager
def _servers(pii: bool) -> Iterator[None]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "GUARDRAIL_NETWORK__LISTEN_PORT": str(PROXY_PORT),
            "GUARDRAIL_NETWORK__OPENAI_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
            "GUARDRAIL_NETWORK__ANTHROPIC_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}",
            "GUARDRAIL_SCANNING__ENABLE_PII": "true" if pii else "false",
            "GUARDRAIL_LOGGING__FORMAT": "json",
        }
    )
    procs: list[subprocess.Popen[bytes]] = []
    logs = []
    try:
        for name, cmd in (
            (
                "mock",
                [
                    sys.executable, "-m", "uvicorn", "benchmarks.mock_upstream:app",
                    "--port", str(MOCK_PORT), "--log-level", "warning", "--no-access-log",
                ],
            ),
            ("proxy", [sys.executable, "-m", "llm_guardrail_proxy"]),
        ):
            log = (LOG_DIR / f"{name}.log").open("wb")
            logs.append(log)
            procs.append(
                subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            )
        _wait_healthy(f"http://127.0.0.1:{MOCK_PORT}/healthz")
        _wait_healthy(f"http://127.0.0.1:{PROXY_PORT}/healthz")
        yield
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log in logs:
            log.close()


def run(
    requests: int, concurrencies: Sequence[int], sizes: dict[str, int], pii: bool
) -> list[Result]:
    enc = tiktoken.get_encoding("o200k_base")
    targets = {
        "direct": f"http://127.0.0.1:{MOCK_PORT}",
        "proxy": f"http://127.0.0.1:{PROXY_PORT}",
    }
    results: list[Result] = []
    with _servers(pii):
        for label, tokens in sizes.items():
            text = _prompt(tokens)
            body: dict[str, object] = {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": text}],
            }
            for concurrency in concurrencies:
                for target, url in targets.items():
                    latencies, elapsed = asyncio.run(
                        _run_load(url, body, requests, concurrency, warmup=min(50, requests))
                    )
                    latencies.sort()
                    results.append(
                        Result(
                            prompt=label,
                            prompt_tokens=len(enc.encode(text)),
                            concurrency=concurrency,
                            target=target,
                            requests=len(latencies),
                            p50_ms=_percentile(latencies, 0.50),
                            p95_ms=_percentile(latencies, 0.95),
                            p99_ms=_percentile(latencies, 0.99),
                            mean_ms=statistics.fmean(latencies),
                            rps=len(latencies) / elapsed,
                        )
                    )
    return results


def render_markdown(results: Sequence[Result]) -> str:
    """Two tables: added latency at concurrency 1, throughput at every level.

    Latency deltas are only reported at concurrency 1. Under load the client
    shares the CPU with both servers and its own queueing dominates the tail,
    so a "proxy p99 minus direct p99" figure stops meaning anything.
    """

    by_key = {(r.prompt, r.concurrency, r.target): r for r in results}
    prompts = list(dict.fromkeys(r.prompt for r in results))
    levels = sorted({r.concurrency for r in results})

    lines = [
        "| Prompt | Direct p50 | Proxy p50 | Added p50 | Added p95 | Added p99 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for prompt in prompts:
        d, p = by_key.get((prompt, 1, "direct")), by_key.get((prompt, 1, "proxy"))
        if d is None or p is None:
            continue
        lines.append(
            f"| {prompt} (~{p.prompt_tokens:,} tokens) | {d.p50_ms:.2f} ms | {p.p50_ms:.2f} ms "
            f"| +{p.p50_ms - d.p50_ms:.2f} ms | +{p.p95_ms - d.p95_ms:.2f} ms "
            f"| +{p.p99_ms - d.p99_ms:.2f} ms |"
        )

    header = " | ".join(f"c={c} direct | c={c} proxy" for c in levels)
    lines += ["", f"| Prompt | {header} |", "|---|" + "---:|" * (2 * len(levels))]
    for prompt in prompts:
        cells = []
        for c in levels:
            for target in ("direct", "proxy"):
                r = by_key.get((prompt, c, target))
                cells.append(f"{r.rps:,.0f} req/s" if r else "-")
        lines.append(f"| {prompt} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=1000, help="Requests per scenario.")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 16])
    parser.add_argument("--pii", action="store_true", help="Enable PII scanning in the proxy.")
    parser.add_argument("--json", type=Path, default=None, help="Also write raw results here.")
    args = parser.parse_args(argv)

    sizes = {"small": 50, "medium": 1_000, "large": 6_000}
    results = run(args.requests, args.concurrency, sizes, args.pii)

    env = _environment()
    sys.stdout.write(render_markdown(results) + "\n\n")
    sys.stdout.write(
        f"{args.requests} requests per scenario; PII {'on' if args.pii else 'off'}; "
        f"Python {env['python']} on {env['platform']} ({env['cpu_count']} CPUs).\n"
    )
    if args.json:
        args.json.write_text(
            json.dumps({"environment": env, "results": [asdict(r) for r in results]}, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
