"""Tests for the ``llm-guardrail-scan`` CLI.

Most cases drive the CLI by direct function call against ``main(argv)``,
which returns an integer exit code. A single subprocess-level test
guards the console-script wiring end-to-end so that broken setuptools
metadata is caught early.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from llm_guardrail_proxy.cli.scan import (
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_REJECTED,
    main,
)


# --------------------------------------------------------------- happy path


class TestCleanInput:
    def test_clean_text_returns_zero(self, capsys: pytest.CaptureFixture) -> None:
        exit_code = main(["--text", "summarise this Python module", "--model", "gpt-4o"])
        out = capsys.readouterr().out
        assert exit_code == EXIT_OK
        payload = json.loads(out)
        assert payload["verdict"] == "allowed"
        assert payload["rejecting_middleware"] is None

    def test_clean_file_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        prompt_file = tmp_path / "prompt.json"
        prompt_file.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            ),
            encoding="utf-8",
        )
        exit_code = main(["--file", str(prompt_file)])
        out = capsys.readouterr().out
        assert exit_code == EXIT_OK
        assert json.loads(out)["verdict"] == "allowed"


# ---------------------------------------------------------------- rejection


class TestSecretRejection:
    def test_aws_secret_in_text_returns_one(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        exit_code = main(
            ["--text", "deploy with AKIAABCDEFGHIJKLMNOP", "--model", "gpt-4o"]
        )
        out = capsys.readouterr().out
        assert exit_code == EXIT_REJECTED
        payload = json.loads(out)
        assert payload["verdict"] == "rejected"
        assert payload["rejecting_middleware"] == "secret_scan"
        # Reject payload carries the structured detail expected by CI
        # pipelines.
        assert payload["reject"]["reason"] == "secret_exposure_detected"

    def test_aws_secret_in_file_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        prompt_file = tmp_path / "prompt.json"
        prompt_file.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "leak: AKIAABCDEFGHIJKLMNOP"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        exit_code = main(["--file", str(prompt_file)])
        out = capsys.readouterr().out
        assert exit_code == EXIT_REJECTED
        assert json.loads(out)["rejecting_middleware"] == "secret_scan"

    def test_output_does_not_contain_raw_secret(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        # The whole CI surface — stdout, exit code, anything a developer
        # might cat — must not re-leak the AWS-shaped fixture.
        main(["--text", "leak: AKIAABCDEFGHIJKLMNOP", "--model", "gpt-4o"])
        captured = capsys.readouterr()
        assert "AKIAABCDEFGHIJKLMNOP" not in captured.out
        assert "AKIAABCDEFGHIJKLMNOP" not in captured.err


# ------------------------------------------------------------ provider detect


class TestProviderDetection:
    def test_anthropic_via_top_level_system(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        prompt_file = tmp_path / "prompt.json"
        prompt_file.write_text(
            json.dumps(
                {
                    "model": "claude-3-5-sonnet-latest",
                    "system": "be brief",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ),
            encoding="utf-8",
        )
        exit_code = main(["--file", str(prompt_file)])
        # Adapter resolved cleanly; no input error.
        assert exit_code == EXIT_OK

    def test_anthropic_via_model_prefix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        # No ``system`` field — provider must still resolve via the
        # ``claude``-prefixed model name.
        prompt_file = tmp_path / "prompt.json"
        prompt_file.write_text(
            json.dumps(
                {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ),
            encoding="utf-8",
        )
        exit_code = main(["--file", str(prompt_file)])
        assert exit_code == EXIT_OK


# ----------------------------------------------------------------- tokens


class TestTokenomicsOptIn:
    def test_default_skips_tokenomics(self, capsys: pytest.CaptureFixture) -> None:
        # Even an enormous prompt passes when --tokens is not set.
        big = "word " * 50_000
        exit_code = main(["--text", big, "--model", "gpt-4o"])
        assert exit_code == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        # No tokenomics annotation should be present.
        assert "tokenomics" not in payload["annotations"]

    def test_tokens_flag_enables_check(self, capsys: pytest.CaptureFixture) -> None:
        big = "word " * 200
        exit_code = main(
            [
                "--text",
                big,
                "--model",
                "gpt-4o",
                "--tokens",
                "--max-tokens",
                "10",
            ]
        )
        assert exit_code == EXIT_REJECTED
        payload = json.loads(capsys.readouterr().out)
        assert payload["rejecting_middleware"] == "tokenomics"

    def test_max_cost_must_be_decimal(self, capsys: pytest.CaptureFixture) -> None:
        exit_code = main(
            [
                "--text",
                "hi",
                "--model",
                "gpt-4o",
                "--tokens",
                "--max-cost",
                "not-a-number",
            ]
        )
        assert exit_code == EXIT_INPUT_ERROR


# ------------------------------------------------------------ input errors


class TestInputErrors:
    def test_missing_file_returns_input_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        exit_code = main(["--file", str(tmp_path / "does-not-exist.json")])
        assert exit_code == EXIT_INPUT_ERROR
        assert "could not read" in capsys.readouterr().err

    def test_malformed_json_returns_input_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        exit_code = main(["--file", str(bad)])
        assert exit_code == EXIT_INPUT_ERROR


# ---------------------------------------------------------------- formats


class TestFormat:
    def test_text_format_is_human_readable(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        exit_code = main(
            [
                "--text",
                "leak AKIAABCDEFGHIJKLMNOP",
                "--model",
                "gpt-4o",
                "--format",
                "text",
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == EXIT_REJECTED
        # The first line carries the verdict — pre-commit hook captures
        # often display only the first line.
        first = out.splitlines()[0]
        assert "FAIL" in first
        assert "secret_scan" in first

    def test_json_is_default_format(self, capsys: pytest.CaptureFixture) -> None:
        main(["--text", "hi", "--model", "gpt-4o"])
        out = capsys.readouterr().out
        # Round-trip — confirms valid JSON.
        json.loads(out)


# ---------------------------------------------------- console-script wiring


class TestConsoleScript:
    def test_subprocess_invocation_exits_with_expected_code(
        self, tmp_path: Path
    ) -> None:
        # Drives the installed entry point through subprocess so the
        # pyproject.toml ``[project.scripts]`` wiring is exercised.
        prompt = tmp_path / "prompt.json"
        prompt.write_text(
            json.dumps(
                {
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "user", "content": "leak AKIAABCDEFGHIJKLMNOP"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, "-m", "llm_guardrail_proxy.cli", "--file", str(prompt)],
            capture_output=True,
            text=True,
            check=False,
        )
        # ``-m`` exercises the cli package's ``__main__`` shim.
        assert proc.returncode == EXIT_REJECTED
        payload = json.loads(proc.stdout)
        assert payload["rejecting_middleware"] == "secret_scan"
