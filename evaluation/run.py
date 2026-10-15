"""Run the evaluation and write the numbers the report is built from.

    python evaluation/run.py                # 3 runs per configuration
    python evaluation/run.py --runs 5
    python evaluation/run.py --skip-extraction

Repeats matter. Temperature is 0, but the same diagram does not produce the
same review twice, and a single run of an LLM pipeline measures the run rather
than the pipeline. Everything reported here is a mean over repeats with the
spread shown, because a number without a spread invites more confidence than
it has earned.

Findings are matched to ground truth on evidence and wording together. Matching
on wording alone would score a reviewer that names the right problem on the
wrong service as correct; matching on evidence alone would score any complaint
about a flagged service as correct.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.agents.extraction import extract_graph
from backend.agents.supervisor import review
from backend.core.aws import usage
from backend.core.models import Finding, Graph
from evaluation.baselines import review_generalist
from evaluation.compare import score as score_graph

DATASETS = Path(__file__).resolve().parent / "datasets"
RESULTS = Path(__file__).resolve().parent / "results"

STOPWORDS = frozenset(
    "a an and are as at be by for from has in is its no not of on or the to with "
    "without this that enabled used using it their there which when while".split()
)


def topic(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 1)


def matches(finding: Finding, expected: dict) -> bool:
    """Whether a reported finding corresponds to an expected one."""
    if not (set(finding.evidence) & set(expected["evidence"])):
        return False

    reported, wanted = topic(finding.issue), topic(expected["issue"])
    if not reported or not wanted:
        return False

    # Against the shorter of the two, so a terse finding is not punished for
    # saying the same thing in fewer words.
    return len(reported & wanted) / min(len(reported), len(wanted)) >= 0.3


@dataclass
class RunResult:
    findings: int
    rejected: int
    matched: int
    expected: int
    pillars: set[str]
    seconds: float
    usd: float
    tokens: int

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 1.0

    @property
    def precision(self) -> float:
        return self.matched / self.findings if self.findings else 1.0

    @property
    def false_positives(self) -> int:
        return self.findings - self.matched


@dataclass
class Aggregate:
    label: str
    runs: list[RunResult] = field(default_factory=list)

    def mean(self, attr: str) -> float:
        values = [getattr(r, attr) for r in self.runs]
        return statistics.mean(values) if values else 0.0

    def spread(self, attr: str) -> str:
        values = [getattr(r, attr) for r in self.runs]
        return f"{min(values):g}–{max(values):g}" if values else "–"

    def row(self) -> dict:
        return {
            "configuration": self.label,
            "findings": round(self.mean("findings"), 1),
            "findings_range": self.spread("findings"),
            "matched": round(self.mean("matched"), 1),
            "false_positives": round(self.mean("false_positives"), 1),
            "recall": round(self.mean("recall"), 2),
            "precision": round(self.mean("precision"), 2),
            "pillars_covered": round(statistics.mean([len(r.pillars) for r in self.runs]), 1),
            "seconds": round(self.mean("seconds"), 1),
            "usd_per_review": round(self.mean("usd"), 5),
            "tokens": int(self.mean("tokens")),
        }


def truth_graph(truth: dict) -> Graph:
    """The confirmed graph — what the human gate produces on a correct review."""
    return Graph(
        nodes=[
            {"id": n["id"], "service": n["service"], "label": n["label"], "group": n.get("cluster")}
            for n in truth["nodes"]
        ],
        edges=[
            {"source": e["from"], "target": e["to"], "label": e.get("label")}
            for e in truth["edges"]
        ],
    )


def measure(fn, truth: dict) -> RunResult:
    usage.reset()
    started = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - started

    expected = truth["expected_findings"]
    matched = sum(
        1 for want in expected if any(matches(f, want) for f in result.findings)
    )

    return RunResult(
        findings=len(result.findings),
        rejected=len(result.rejected),
        matched=matched,
        expected=len(expected),
        pillars={str(f.pillar) for f in result.findings},
        seconds=elapsed,
        usd=usage.usd,
        tokens=usage.input_tokens + usage.output_tokens + usage.embed_tokens,
    )


def evaluate_extraction(runs: int) -> list[dict]:
    """How well the diagram is read, before any human sees it."""
    rows = []
    for case in sorted(DATASETS.glob("0*")):
        image = (case / "diagram.png").read_bytes()
        scores, seconds = [], []
        for _ in range(runs):
            usage.reset()
            started = time.perf_counter()
            graph = extract_graph(image)
            seconds.append(time.perf_counter() - started)
            scores.append(score_graph(case, graph))

        rows.append(
            {
                "case": case.name,
                "node_recall": round(statistics.mean(s.node_recall for s in scores), 2),
                "spurious_nodes": round(statistics.mean(len(s.nodes_spurious) for s in scores), 1),
                "edge_recall": round(statistics.mean(s.edge_recall for s in scores), 2),
                "hallucinated_edge_rate": round(
                    statistics.mean(s.hallucinated_edge_rate for s in scores), 2
                ),
                "seconds": round(statistics.mean(seconds), 1),
            }
        )
        print(f"  {rows[-1]['case']:<20} "
              f"nodes {rows[-1]['node_recall']:.0%}  "
              f"edges {rows[-1]['edge_recall']:.0%}  "
              f"hallucinated {rows[-1]['hallucinated_edge_rate']:.0%}")
    return rows


def evaluate_review(runs: int) -> list[dict]:
    """Review quality on confirmed graphs, across the three configurations."""
    configurations = {
        "B1 — specialists, no verification": lambda g: review(g, verify_findings=False),
        "B2 — one generalist, verified": lambda g: review_generalist(g),
        "Full — specialists, verified": lambda g: review(g),
    }

    rows = []
    for case in sorted(DATASETS.glob("0*")):
        truth = json.loads((case / "truth.json").read_text())
        graph = truth_graph(truth)
        print(f"\n  {case.name} (expects {len(truth['expected_findings'])} findings)")

        for label, fn in configurations.items():
            aggregate = Aggregate(label)
            for _ in range(runs):
                # Bind both in the default arguments: a bare closure would
                # capture the loop variables by reference.
                aggregate.runs.append(
                    measure(lambda f=fn, g=graph: f(g), truth)
                )

            row = {"case": case.name, **aggregate.row()}
            rows.append(row)
            print(
                f"    {label:<36} "
                f"found {row['findings']:>4}  "
                f"matched {row['matched']:>3}/{len(truth['expected_findings'])}  "
                f"fp {row['false_positives']:>4}  "
                f"{row['seconds']:>4.1f}s  ${row['usd_per_review']:.5f}"
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="repeats per configuration")
    parser.add_argument("--skip-extraction", action="store_true")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    report: dict = {"runs_per_configuration": args.runs}

    if not args.skip_extraction:
        print("Extraction (how the diagram is read, before the human gate):")
        report["extraction"] = evaluate_extraction(args.runs)

    print("\nReview (on confirmed graphs):")
    report["review"] = evaluate_review(args.runs)

    out = RESULTS / "evaluation.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
