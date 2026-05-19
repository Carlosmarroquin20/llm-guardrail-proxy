"""Smoke test for the Phase 5 shift-left CLI.

Invokes the installed ``llm-guardrail-scan`` via ``python -m`` against
two fixture prompts: one clean, one carrying a synthetic AWS-shaped
secret. Asserts the exit codes match the shift-left contract and that
the raw secret never appears in the CI-facing output.

Run with: ``python scripts/smoke_phase5.py``
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _python() -> str:
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def _scan(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_python(), "-m", "llm_guardrail_proxy.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )


def _run_smoke() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # --- Fixture A: clean OpenAI Chat prompt.
        clean = tmp_path / "clean.json"
        clean.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "Summarise the TLS handshake."}
                    ],
                }
            ),
            encoding="utf-8",
        )

        # --- Fixture B: prompt carrying a synthetic AWS access key.
        leak = tmp_path / "leak.json"
        leak.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": "deploy with AKIAABCDEFGHIJKLMNOP",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        # --- Run A.
        result_clean = _scan("--file", str(clean))
        assert result_clean.returncode == 0, result_clean.stdout + result_clean.stderr
        payload_clean = json.loads(result_clean.stdout)
        assert payload_clean["verdict"] == "allowed"
        print(f"  clean prompt : exit=0 verdict=allowed")

        # --- Run B.
        result_leak = _scan("--file", str(leak), "--format", "text")
        assert result_leak.returncode == 1, result_leak.stdout + result_leak.stderr
        # Re-leakage invariant — the raw secret must not appear anywhere
        # in the CI-visible streams.
        assert "AKIAABCDEFGHIJKLMNOP" not in result_leak.stdout
        assert "AKIAABCDEFGHIJKLMNOP" not in result_leak.stderr
        first_line = result_leak.stdout.splitlines()[0]
        assert "FAIL" in first_line and "secret_scan" in first_line
        print(f"  leaking prompt: exit=1 ({first_line.strip()})")

        # --- Run C: tokenomics opt-in. Tiny limit forces rejection.
        result_tokens = _scan(
            "--text",
            "word " * 200,
            "--model",
            "gpt-4o",
            "--tokens",
            "--max-tokens",
            "10",
        )
        assert result_tokens.returncode == 1
        payload_tok = json.loads(result_tokens.stdout)
        assert payload_tok["rejecting_middleware"] == "tokenomics"
        print(f"  tokenomics    : exit=1 rejecting_middleware=tokenomics")

        # --- Run D: malformed input → exit 2.
        malformed = tmp_path / "bad.json"
        malformed.write_text("{not-json", encoding="utf-8")
        result_bad = _scan("--file", str(malformed))
        assert result_bad.returncode == 2
        print(f"  malformed json: exit=2")

        print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(_run_smoke())
