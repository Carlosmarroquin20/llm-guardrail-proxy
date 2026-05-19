"""End-to-end smoke test for the Phase 4c stats endpoint.

Boots a real uvicorn process, generates a small mix of allowed and
rejected requests, then queries ``/stats/summary`` and ``/stats/recent``
and asserts that the aggregates reflect the synthetic workload.

Run with: ``python scripts/smoke_phase4c.py``
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"
LOG_FILE = VAR_DIR / "smoke-phase4c.log"
PORT = 8767
BASE_URL = f"http://127.0.0.1:{PORT}"


def _wait_for_health(client_module, deadline_s: float = 10.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            r = client_module.get(f"{BASE_URL}/healthz", timeout=0.5)
            if r.status_code == 200:
                return
        except client_module.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError("proxy did not become healthy within deadline")


def _run_smoke() -> int:
    VAR_DIR.mkdir(exist_ok=True)
    if LOG_FILE.exists():
        LOG_FILE.unlink()

    env = os.environ.copy()
    env["GUARDRAIL_LISTEN_PORT"] = str(PORT)
    env["GUARDRAIL_ENABLE_PII_SCANNING"] = "false"
    env["GUARDRAIL_LOG_FORMAT"] = "json"
    # Stats endpoint is default-on, but pin it explicitly for clarity.
    env["GUARDRAIL_ENABLE_STATS_ENDPOINT"] = "true"

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = ROOT / ".venv" / "bin" / "python"

    with LOG_FILE.open("w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            [str(python), "-m", "llm_guardrail_proxy"],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
        )
        try:
            import httpx

            _wait_for_health(httpx)
            print(f"server up on {BASE_URL}")

            # Synthetic workload: three rejections (different shapes), zero
            # successful forwards (no real upstream is available in the
            # smoke environment). The audit ring captures only requests
            # that reach the pipeline — malformed bodies bypass it.
            for _ in range(3):
                r = httpx.post(
                    f"{BASE_URL}/v1/chat/completions",
                    json={
                        "model": "gpt-4o",
                        "messages": [
                            {"role": "user", "content": "leak AKIAABCDEFGHIJKLMNOP"}
                        ],
                    },
                    timeout=5,
                )
                assert r.status_code == 403

            # Brief settle for the audit sink chain.
            time.sleep(0.3)

            # --- /stats/summary --------------------------------------
            summary = httpx.get(f"{BASE_URL}/stats/summary", timeout=5).json()
            assert summary["total_requests"] == 3, summary
            assert summary["allowed"] == 0
            assert summary["rejected"] == 3
            assert summary["rejection_rate"] == 1.0
            assert summary["rejections_by_middleware"] == {"secret_scan": 3}
            assert summary["requests_by_model"] == {"gpt-4o": 3}
            assert summary["findings_by_scanner"] == {"secret_scan": 3}
            print(
                f"  /stats/summary: total={summary['total_requests']} "
                f"rejected={summary['rejected']} "
                f"by_mw={summary['rejections_by_middleware']}"
            )

            # --- /stats/recent ---------------------------------------
            recent = httpx.get(
                f"{BASE_URL}/stats/recent?limit=10", timeout=5
            ).json()
            assert len(recent) == 3
            assert all(r["verdict"] == "rejected" for r in recent)
            assert all(
                r["rejecting_middleware"] == "secret_scan" for r in recent
            )
            # The raw secret must not appear in the response payload — same
            # invariant the JSONL and DuckDB sinks uphold.
            raw_body = httpx.get(
                f"{BASE_URL}/stats/recent?limit=10", timeout=5
            ).text
            assert "AKIAABCDEFGHIJKLMNOP" not in raw_body
            print(f"  /stats/recent: {len(recent)} records, no raw leakage")

            # --- limit query parameter -------------------------------
            small = httpx.get(
                f"{BASE_URL}/stats/recent?limit=1", timeout=5
            ).json()
            assert len(small) == 1
            print(f"  /stats/recent?limit=1: returned 1 record")

            print("\nSMOKE OK")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(_run_smoke())
