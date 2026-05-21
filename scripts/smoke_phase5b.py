"""Smoke test for the Phase 5b pre-commit integration.

Drives ``pre-commit run`` against a synthetic consumer repo that
declares an inline ``repo: local`` hook with the *same* entry/language/
arguments the published ``.pre-commit-hooks.yaml`` exposes. This avoids
the chicken-and-egg of ``try-repo`` (which clones the repo and requires
``.pre-commit-hooks.yaml`` to be present in HEAD) while still validating
the runtime contract consumers will observe:

* clean fixture → hook PASS, exit 0
* leaking fixture → hook FAIL, exit non-zero
* raw secret never appears in the hook's printed output

The published hook YAML shape itself is pinned by
``tests/test_phase5b_metadata.py``, and the composite GitHub Action is
exercised by the CI ``dogfood`` job. Together the three layers cover
the same ground as a real ``try-repo`` invocation.

Run with: ``python scripts/smoke_phase5b.py``
"""

from __future__ import annotations

import json
import os
import shutil
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


def _scan_executable() -> str:
    """Resolve the absolute path to the installed ``llm-guardrail-scan``.

    pre-commit's ``language: system`` hook expects an entry resolvable
    via the PATH of the subshell it spawns. The .venv's Scripts
    directory is not necessarily on PATH inside that subshell on
    Windows, so we hand the script the absolute path explicitly.

    Path is returned in POSIX form (forward slashes) because the YAML
    parser strips unrecognised backslash escapes — a Windows path like
    ``D:\\CODE\\Projects\\...`` would be silently mangled to
    ``DCODEProjects...``.
    """

    candidates = [
        ROOT / ".venv" / "Scripts" / "llm-guardrail-scan.exe",
        ROOT / ".venv" / "bin" / "llm-guardrail-scan",
    ]
    for c in candidates:
        if c.exists():
            return c.as_posix()
    raise RuntimeError(
        "could not locate the installed llm-guardrail-scan console script"
    )


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _run_pre_commit(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_python(), "-m", "pre_commit", "run", "--all-files"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _run_smoke() -> int:
    if shutil.which("git") is None:
        print("ERROR: git is not on PATH")
        return 1

    scan_entry = _scan_executable()

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "consumer-repo"
        repo.mkdir()

        _git("init", "-q", cwd=repo)
        _git("config", "user.email", "smoke@example.test", cwd=repo)
        _git("config", "user.name", "Smoke", cwd=repo)
        _git("config", "commit.gpgsign", "false", cwd=repo)

        # Inline ``repo: local`` config — identical entry/language to
        # the published ``.pre-commit-hooks.yaml`` so the runtime
        # contract is the same. We keep ``pass_filenames: true`` (the
        # default) so positional file args reach the CLI's batch mode.
        (repo / ".pre-commit-config.yaml").write_text(
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: llm-guardrail-scan\n"
            "        name: llm-guardrail-scan\n"
            f"        entry: {scan_entry}\n"
            "        language: system\n"
            "        types_or: [json]\n"
            "        require_serial: true\n",
            encoding="utf-8",
        )

        # Fixture A: clean prompt.
        clean = repo / "prompts" / "clean.json"
        clean.parent.mkdir(parents=True)
        clean.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "summarise"}],
                }
            ),
            encoding="utf-8",
        )
        _git("add", "-A", cwd=repo)
        proc = _run_pre_commit(repo)
        if proc.returncode != 0:
            print("clean-fixture run failed unexpectedly:")
            print(proc.stdout)
            print(proc.stderr)
            return 1
        print("  clean prompt : hook PASS")

        # Fixture B: leak.
        leak = repo / "prompts" / "leak.json"
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
        _git("add", "-A", cwd=repo)
        proc = _run_pre_commit(repo)
        assert proc.returncode != 0, (
            f"hook unexpectedly allowed a leaking prompt: stdout={proc.stdout}"
        )
        combined = proc.stdout + proc.stderr
        assert "FAIL" in combined or "secret_scan" in combined, combined
        assert (
            "AKIAABCDEFGHIJKLMNOP" not in combined
        ), "hook output leaked the raw secret"
        print(f"  leaking prompt: hook FAIL (exit={proc.returncode})")

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(_run_smoke())
