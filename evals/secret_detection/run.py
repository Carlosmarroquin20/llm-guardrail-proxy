"""Score the secret scanner against the synthetic corpus.

Usage::

    python -m evals.secret_detection.run              # markdown report
    python -m evals.secret_detection.run --json       # machine-readable
    python -m evals.secret_detection.run --min-recall 0.99 --max-false-block-rate 0.01

Scoring is per *sample and kind*: a positive counts as found when its
expected kind is among the kinds the scanner reports for that text. Any
reported kind that was not expected is a false positive for that kind — this
also catches overlapping patterns that label one credential as two issuers.

"False block rate" is the share of negative samples on which the scanner
reports anything at all. That is the number a developer feels: how often a
harmless prompt is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

from evals.secret_detection.corpus import PLACEHOLDER_GENERATORS, Sample, build_corpus
from llm_guardrail_proxy.proxy.scanning import SecretScanner

_PLACEHOLDER_FAMILIES = frozenset(PLACEHOLDER_GENERATORS)


@dataclass(slots=True)
class KindScore:
    support: int = 0
    true_positives: int = 0
    false_positives: int = 0

    @property
    def false_negatives(self) -> int:
        return self.support - self.true_positives

    @property
    def precision(self) -> float:
        reported = self.true_positives + self.false_positives
        return self.true_positives / reported if reported else 1.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.support if self.support else 1.0


@dataclass(slots=True)
class Report:
    samples: int
    positives: int
    negatives: int
    placeholders: int
    precision: float
    recall: float
    false_block_rate: float
    placeholder_block_rate: float
    per_kind: dict[str, dict[str, float | int]] = field(default_factory=dict)
    missed_by_variant: dict[str, int] = field(default_factory=dict)
    blocked_by_negative_family: dict[str, int] = field(default_factory=dict)


def evaluate(samples: Iterable[Sample], scanner: SecretScanner | None = None) -> Report:
    scanner = scanner or SecretScanner()
    kinds: defaultdict[str, KindScore] = defaultdict(KindScore)
    for pattern in scanner.patterns:
        kinds[pattern.name]  # every catalogued kind appears in the report

    missed: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    n_pos = n_neg = n_placeholder = neg_blocked = placeholder_blocked = 0
    total = 0

    for sample in samples:
        total += 1
        found = {f.kind for f in scanner.scan(sample.text)}

        for kind in sample.expected:
            kinds[kind].support += 1
            if kind in found:
                kinds[kind].true_positives += 1
            else:
                missed[sample.category] += 1
        is_placeholder = sample.category in _PLACEHOLDER_FAMILIES
        # Placeholders are reported on their own and kept out of precision,
        # so the headline number reflects realistic lookalikes only.
        if not is_placeholder:
            for kind in found - sample.expected:
                kinds[kind].false_positives += 1

        if sample.expected:
            n_pos += 1
        elif is_placeholder:
            n_placeholder += 1
            placeholder_blocked += bool(found)
        else:
            n_neg += 1
            neg_blocked += bool(found)
            if found:
                blocked[sample.category] += 1

    tp =sum(k.true_positives for k in kinds.values())
    support = sum(k.support for k in kinds.values())
    fp = sum(k.false_positives for k in kinds.values())

    return Report(
        samples=total,
        positives=n_pos,
        negatives=n_neg,
        placeholders=n_placeholder,
        precision=tp / (tp + fp) if tp + fp else 1.0,
        recall=tp / support if support else 1.0,
        false_block_rate=neg_blocked / n_neg if n_neg else 0.0,
        placeholder_block_rate=placeholder_blocked / n_placeholder if n_placeholder else 0.0,
        per_kind={
            name: {
                "support": s.support,
                "true_positives": s.true_positives,
                "false_negatives": s.false_negatives,
                "false_positives": s.false_positives,
                "precision": round(s.precision, 4),
                "recall": round(s.recall, 4),
            }
            for name, s in sorted(kinds.items())
        },
        missed_by_variant=dict(missed.most_common()),
        blocked_by_negative_family=dict(blocked.most_common()),
    )


def render_markdown(report: Report) -> str:
    lines = [
        f"Samples: {report.samples} "
        f"({report.positives} positive, {report.negatives} lookalike negatives, "
        f"{report.placeholders} placeholders)",
        "",
        f"- Precision: {report.precision:.2%}",
        f"- Recall: {report.recall:.2%}",
        f"- False block rate (lookalikes): {report.false_block_rate:.2%}",
        f"- Placeholder block rate: {report.placeholder_block_rate:.2%}",
        "",
        "| Kind | Support | Recall | False positives |",
        "|---|---:|---:|---:|",
    ]
    for name, s in report.per_kind.items():
        lines.append(f"| `{name}` | {s['support']} | {s['recall']:.2%} | {s['false_positives']} |")
    if report.missed_by_variant:
        lines += ["", "Missed positives by variant:", ""]
        lines += [f"- `{v}`: {n}" for v, n in report.missed_by_variant.items()]
    if report.blocked_by_negative_family:
        lines += ["", "Blocked negatives by family:", ""]
        lines += [f"- `{f}`: {n}" for f, n in report.blocked_by_negative_family.items()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")
    parser.add_argument("--min-precision", type=float, default=None)
    parser.add_argument("--min-recall", type=float, default=None)
    parser.add_argument("--max-false-block-rate", type=float, default=None)
    args = parser.parse_args(argv)

    report = evaluate(build_corpus())
    out = json.dumps(asdict(report), indent=2) if args.json else render_markdown(report)
    sys.stdout.write(out + "\n")

    failures = []
    if args.min_precision is not None and report.precision < args.min_precision:
        failures.append(f"precision {report.precision:.4f} < {args.min_precision}")
    if args.min_recall is not None and report.recall < args.min_recall:
        failures.append(f"recall {report.recall:.4f} < {args.min_recall}")
    if (
        args.max_false_block_rate is not None
        and report.false_block_rate > args.max_false_block_rate
    ):
        failures.append(
            f"false block rate {report.false_block_rate:.4f} > {args.max_false_block_rate}"
        )
    for failure in failures:
        sys.stderr.write(f"FAIL: {failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
