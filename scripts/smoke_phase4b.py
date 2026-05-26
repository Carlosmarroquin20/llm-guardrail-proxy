"""End-to-end smoke test for the Phase 4b sink fan-out.

Drives a real ``uvicorn`` process configured with the composite sink
stack: in-memory ring + structlog + JSONL + DuckDB. Verifies that the
same record is persisted to every destination.

Run with: ``python scripts/smoke_phase4b.py``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"
JSONL = VAR_DIR / "smoke-phase4b-audit.jsonl"
DUCKDB = VAR_DIR / "smoke-phase4b-audit.duckdb"
LOG_FILE = VAR_DIR / "smoke-phase4b.log"
PORT = 8766
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
    for p in (JSONL, DUCKDB, LOG_FILE):
        if p.exists():
            p.unlink()

    env = os.environ.copy()
    env["GUARDRAIL_AUDIT__JSONL_PATH"] = str(JSONL)
    env["GUARDRAIL_AUDIT__DUCKDB_PATH"] = str(DUCKDB)
    env["GUARDRAIL_NETWORK__LISTEN_PORT"] = str(PORT)
    env["GUARDRAIL_SCANNING__ENABLE_PII"] = "false"
    # Force JSON output so the log file is machine-parseable.
    env["GUARDRAIL_LOGGING__FORMAT"] = "json"

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        print("ERROR: could not locate the .venv interpreter")
        return 1

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

            # Single rejection — guaranteed to traverse every sink.
            r = httpx.post(
                f"{BASE_URL}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "leak: AKIAABCDEFGHIJKLMNOP"}
                    ],
                },
                timeout=5,
            )
            assert r.status_code == 403
            rid = r.headers["x-request-id"]
            print(f"  secret-leak: 403 x-request-id={rid}")

            # Let the async writers flush.
            time.sleep(0.4)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    # --- Sink 1: JSONL.
    jsonl_lines = JSONL.read_text(encoding="utf-8").splitlines()
    assert len(jsonl_lines) == 1, f"expected 1 JSONL record, got {len(jsonl_lines)}"
    jsonl_rec = json.loads(jsonl_lines[0])
    assert jsonl_rec["request_id"] == rid
    print(f"  JSONL: 1 record, request_id matches")

    # --- Sink 2: DuckDB.
    import duckdb

    conn = duckdb.connect(str(DUCKDB))
    rows = conn.execute(
        "SELECT request_id, verdict, rejecting_middleware FROM audit_records"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    duck_rid, duck_verdict, duck_mw = rows[0]
    assert duck_rid == rid
    assert duck_verdict == "rejected"
    assert duck_mw == "secret_scan"
    print(f"  DuckDB: 1 row, request_id matches")

    # --- Sink 3: structlog (captured via log file).
    log_lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    audit_events = [
        json.loads(line)
        for line in log_lines
        if line.startswith("{") and '"audit.request"' in line
    ]
    assert len(audit_events) >= 1, (
        f"expected at least 1 audit.request log line, got {len(audit_events)} "
        f"out of {len(log_lines)} total"
    )
    matching = [e for e in audit_events if e.get("request_id") == rid]
    assert matching, "no audit log line carried the matching request_id"
    print(f"  structlog: {len(audit_events)} audit.request event(s), match found")

    # --- Cross-sink consistency.
    assert (
        jsonl_rec["request_id"] == duck_rid == matching[0]["request_id"]
    ), "request_id must agree across every sink"

    # --- Non-re-leakage invariant.
    for blob in (
        JSONL.read_text(encoding="utf-8"),
        LOG_FILE.read_text(encoding="utf-8"),
    ):
        assert "AKIAABCDEFGHIJKLMNOP" not in blob, "raw secret leaked into a sink"

    print("\nSMOKE OK (composite fan-out verified across 3 sinks)")
    return 0


if __name__ == "__main__":
    sys.exit(_run_smoke())
