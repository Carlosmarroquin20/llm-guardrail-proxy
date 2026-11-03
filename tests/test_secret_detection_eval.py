"""Regression gate over the synthetic secret-detection corpus.

The thresholds sit at the current measured values. The corpus is built from
documented formats by the same people who wrote the patterns, so these
numbers say "every documented shape is still covered and no known lookalike
started matching" — not "the scanner catches 100% of real leaks".
"""

from __future__ import annotations

from evals.secret_detection.corpus import build_corpus
from evals.secret_detection.run import evaluate
from llm_guardrail_proxy.proxy.scanning import SecretScanner


def test_corpus_is_deterministic() -> None:
    assert build_corpus() == build_corpus()


def test_every_catalogued_kind_has_positive_samples() -> None:
    report = evaluate(build_corpus())
    assert all(stats["support"] > 0 for stats in report.per_kind.values())


def test_recall_and_precision_do_not_regress() -> None:
    report = evaluate(build_corpus())
    assert report.recall == 1.0, report.missed_by_variant
    assert report.precision == 1.0, report.per_kind


def test_no_lookalike_negative_is_blocked() -> None:
    report = evaluate(build_corpus())
    assert report.false_block_rate == 0.0, report.blocked_by_negative_family


def test_placeholders_are_not_blocked() -> None:
    assert evaluate(build_corpus()).placeholder_block_rate == 0.0


def test_disabling_placeholder_skip_is_visible_in_the_report() -> None:
    # Sanity check that the placeholder families actually exercise the
    # filter; otherwise the test above would pass vacuously.
    report = evaluate(build_corpus(), SecretScanner(skip_placeholders=False))
    assert report.placeholder_block_rate > 0.5
