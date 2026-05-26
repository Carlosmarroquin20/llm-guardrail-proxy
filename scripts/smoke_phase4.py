"""End-to-end smoke test for the Phase 4a audit plane.

Drives a real ``uvicorn`` process — unlike the pytest suite, which uses
``httpx.MockTransport`` and never opens a socket. The script:

1. Boots the proxy as a child process with a JSONL audit destination.
2. Sends three requests with well-known shapes (one rejected by the
   secret scanner, one with a malformed body, one to an unknown path).
3. Reads the JSONL ledger back and asserts the expected emission contract:
   exactly one audit record, produced only for the path that reached the
   pipeline.
4. Terminates the child cleanly regardless of test outcome.

Run with: ``python scripts/smoke_phase4.py``
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
JSONL = VAR_DIR / "smoke-audit.jsonl"
PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


def _wait_for_health(client_module, deadline_s: float = 10.0) -> None:
    """Poll ``/healthz`` until the server responds or ``deadline_s`` elapses."""

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
    if JSONL.exists():
        JSONL.unlink()

    env = os.environ.copy()
    env["GUARDRAIL_AUDIT__JSONL_PATH"] = str(JSONL)
    env["GUARDRAIL_NETWORK__LISTEN_PORT"] = str(PORT)
    # Disable PII to keep the smoke fast; Phase 3b is independently
    # validated by the pytest suite.
    env["GUARDRAIL_SCANNING__ENABLE_PII"] = "false"

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        # POSIX layout fallback (also makes the script CI-portable).
        python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        print("ERROR: could not locate the .venv interpreter")
        return 1

    proc = subprocess.Popen(
        [str(python), "-m", "llm_guardrail_proxy"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )

    try:
        import httpx

        _wait_for_health(httpx)
        print(f"server up on {BASE_URL}")

        # --- Request 1: triggers the secret scanner.
        secret_payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "leak this: AKIAABCDEFGHIJKLMNOP"}
            ],
        }
        r1 = httpx.post(f"{BASE_URL}/v1/chat/completions", json=secret_payload, timeout=5)
        rid_1 = r1.headers.get("x-request-id")
        assert r1.status_code == 403, f"expected 403, got {r1.status_code}"
        assert rid_1, "missing x-request-id header"
        body1 = r1.json()
        assert body1["error"] == "secret_exposure_detected"
        assert body1["middleware"] == "secret_scan"
        print(f"  secret-leak  : {r1.status_code} x-request-id={rid_1}")

        # --- Request 2: malformed body — must NOT produce an audit record.
        r2 = httpx.post(
            f"{BASE_URL}/v1/chat/completions",
            content=b"not json",
            headers={"content-type": "application/json"},
            timeout=5,
        )
        assert r2.status_code == 400
        print(f"  malformed    : {r2.status_code} (no audit expected)")

        # --- Request 3: unknown path — must NOT produce an audit record.
        r3 = httpx.post(f"{BASE_URL}/v1/audio/transcriptions", json={}, timeout=5)
        assert r3.status_code == 404
        print(f"  unknown-path : {r3.status_code} (no audit expected)")

        # Brief settle for the async JSONL writer.
        time.sleep(0.3)

        if not JSONL.exists():
            print("ERROR: audit JSONL file was not created")
            return 1

        lines = JSONL.read_text(encoding="utf-8").splitlines()
        print(f"\naudit records emitted: {len(lines)}")
        for raw in lines:
            rec = json.loads(raw)
            mw = rec.get("rejecting_middleware")
            findings_summary = ", ".join(
                f"{f['scanner']}/{f['kind']}({f['preview']})"
                for f in rec.get("findings", [])
            )
            print(
                f"  - verdict={rec['verdict']} "
                f"middleware={mw} "
                f"model={rec['model']} "
                f"latency_ms={rec['latency_ms']:.2f} "
                f"findings=[{findings_summary}]"
            )

        # Contract assertions.
        assert len(lines) == 1, f"expected 1 audit record, got {len(lines)}"
        rec = json.loads(lines[0])
        assert rec["verdict"] == "rejected"
        assert rec["rejecting_middleware"] == "secret_scan"
        assert rec["reject_status_code"] == 403
        assert rec["upstream_status_code"] is None
        assert str(rec["request_id"]) == rid_1
        assert len(rec["findings"]) == 1
        assert rec["findings"][0]["kind"] == "aws_access_key_id"
        # Re-leakage check — the JSONL must never contain the raw secret.
        leaked = JSONL.read_text(encoding="utf-8")
        assert "AKIAABCDEFGHIJKLMNOP" not in leaked, "audit re-leaked the raw secret"

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
