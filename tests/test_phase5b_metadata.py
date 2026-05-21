"""Validation tests for the Phase 5b metadata files.

These guard against silent breakage of the consumer-facing contracts:
``.pre-commit-hooks.yaml`` (what downstream repos reference) and
``action.yml`` (what GitHub Actions consumers call). A malformed YAML
here would only surface in a consumer's repository — never as a test
failure in this one, unless we pin the shape here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------- pre-commit


class TestPreCommitHooks:
    @pytest.fixture(scope="class")
    def hooks(self) -> list[dict]:
        text = (REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        assert isinstance(loaded, list), (
            ".pre-commit-hooks.yaml must be a YAML list at the top level"
        )
        return loaded

    def test_single_hook_definition(self, hooks: list[dict]) -> None:
        assert len(hooks) == 1

    def test_hook_id_matches_console_script(self, hooks: list[dict]) -> None:
        # The hook id is the user-facing identifier in consumer configs;
        # changing it without coordination breaks every adopter.
        assert hooks[0]["id"] == "llm-guardrail-scan"

    def test_entry_invokes_installed_console_script(
        self, hooks: list[dict]
    ) -> None:
        assert hooks[0]["entry"] == "llm-guardrail-scan"
        assert hooks[0]["language"] == "python"

    def test_filenames_are_passed_to_the_hook(self, hooks: list[dict]) -> None:
        # pre-commit defaults to True; the YAML is permitted to omit it,
        # but if it sets a value it must remain True. Either way the
        # CLI's batch mode is the contract that makes pre-commit work.
        assert hooks[0].get("pass_filenames", True) is True

    def test_hook_filters_to_json_files(self, hooks: list[dict]) -> None:
        # Consumers can override this; the default targets prompt files
        # without firing on every staged file in the repo.
        assert "json" in hooks[0].get("types_or", [])


# --------------------------------------------------- composite action


class TestActionYaml:
    @pytest.fixture(scope="class")
    def action(self) -> dict:
        text = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        assert isinstance(loaded, dict)
        return loaded

    def test_action_is_composite(self, action: dict) -> None:
        # Composite actions are the only kind that can be referenced by
        # the ``uses:`` syntax without a Docker dependency.
        assert action["runs"]["using"] == "composite"

    def test_required_inputs_are_documented(self, action: dict) -> None:
        # Every consumer-facing input must carry a description so the
        # GitHub Marketplace docs render correctly.
        for name, spec in action.get("inputs", {}).items():
            assert isinstance(spec, dict), f"input {name!r} must be a mapping"
            assert spec.get("description"), (
                f"input {name!r} is missing a description"
            )

    def test_action_steps_install_then_scan(self, action: dict) -> None:
        steps = action["runs"]["steps"]
        # The contract is: setup → install → scan. Reordering breaks
        # cache semantics that downstream workflows rely on.
        names = [s.get("name") for s in steps]
        assert names == [
            "Set up Python",
            "Install llm-guardrail-proxy",
            "Scan staged files",
        ]


# --------------------------------------------------------------- ci


class TestInternalCi:
    @pytest.fixture(scope="class")
    def workflow(self) -> dict:
        text = (
            REPO_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
        assert isinstance(loaded, dict)
        return loaded

    def test_workflow_runs_on_main(self, workflow: dict) -> None:
        # ``on`` is a YAML reserved word that PyYAML parses as ``True``;
        # the field exists under either spelling depending on PyYAML
        # version, so accept both.
        triggers = workflow.get("on") or workflow.get(True)
        assert triggers is not None
        assert "main" in triggers["push"]["branches"]

    def test_python_matrix_covers_floor(self, workflow: dict) -> None:
        versions = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
        assert "3.10" in versions, (
            "CI must cover the project's stated Python floor (3.10) per CLAUDE.md"
        )

    def test_pii_job_downloads_spacy_model(self, workflow: dict) -> None:
        # The [pii] extra is useless without the spaCy model; CI must
        # install it before running the suite or every PII test would
        # be silently skipped.
        steps = workflow["jobs"]["test-with-pii"]["steps"]
        commands = " ".join(s.get("run", "") for s in steps)
        assert "spacy download en_core_web_sm" in commands
