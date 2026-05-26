"""Smoke test for the HTML dashboard at ``/stats/dashboard``.

Boots a real uvicorn process, drives a small synthetic workload through
the proxy, fetches ``/stats/dashboard`` like a browser would, and
verifies:

* The response is HTML (not JSON, not a 404).
* The page contains the DOM anchors the embedded JS depends on.
* The page never references any third-party origin — zero-egress
  applies to the operator-facing UI too.
* The dashboard's polling endpoints (``/stats/summary`` and
  ``/stats/recent``) are reachable and return the records the dashboard
  will display.

Run with: ``python scripts/smoke_dashboard.py``
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"
LOG_FILE = VAR_DIR / "smoke-dashboard.log"
PORT = 8768
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
    env["GUARDRAIL_NETWORK__LISTEN_PORT"] = str(PORT)
    env["GUARDRAIL_SCANNING__ENABLE_PII"] = "false"
    env["GUARDRAIL_LOGGING__FORMAT"] = "json"

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

            # Seed the audit ring so the dashboard has something to show.
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

            time.sleep(0.3)

            # --- HTML.
            resp = httpx.get(f"{BASE_URL}/stats/dashboard", timeout=5)
            assert resp.status_code == 200
            ctype = resp.headers["content-type"]
            assert ctype.startswith("text/html"), ctype
            body = resp.text
            for anchor in (
                'id="summary-cards"',
                'id="recent"',
                "/stats/summary",
                "/stats/recent",
            ):
                assert anchor in body, f"missing dashboard anchor {anchor!r}"
            assert "http://" not in body and "https://" not in body, (
                "dashboard references an external origin — zero-egress breach"
            )
            # The raw secret must not appear in the HTML either; even
            # though the dashboard pulls live data via fetch, the
            # initial document should be free of leakage.
            assert "AKIAABCDEFGHIJKLMNOP" not in body
            print(f"  /stats/dashboard: 200, {len(body)} bytes of HTML")

            # --- JSON endpoints the dashboard polls.
            summary = httpx.get(f"{BASE_URL}/stats/summary", timeout=5).json()
            recent = httpx.get(
                f"{BASE_URL}/stats/recent?limit=25", timeout=5
            ).json()
            assert summary["total_requests"] == 3
            assert len(recent) == 3
            print(
                f"  /stats/summary  : total={summary['total_requests']} "
                f"rejected={summary['rejected']}"
            )
            print(f"  /stats/recent   : {len(recent)} records")

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
