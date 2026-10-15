"""Score an extracted graph against the ground truth of a test case.

Reading a diagram is three abilities, not one, and they fail separately:

  finding the boxes       does it see that something is there
  naming the services     does it know what that something is
  tracing the arrows      does it see how they connect

Measuring them together hides which one broke. The isometric test case makes
that concrete: all ten boxes are found and not one service is identified, which
a single node score reports as zero — indistinguishable from seeing nothing at
all.

So boxes are matched on their labels, services on the service name, and edges
through the box mapping rather than through service names. The last point
matters: with every service reported as "Unknown", service pairs collapse into
each other and eight distinct edges count as four.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from backend.core.models import Graph

# Names for the same service. Kept deliberately small: every entry is a way for
# a wrong answer to be scored as right, so it only covers AWS's own synonyms.
ALIASES = {
    "elb": "load balancer",
    "alb": "load balancer",
    "application load balancer": "load balancer",
    "elastic load balancing": "load balancer",
    "cloudwatch logs": "cloudwatch",
    "amazon s3": "s3",
    "s3 bucket": "s3",
    "dynamodb table": "dynamodb",
    "rds postgresql": "rds",
    "api gateway": "apigateway",
    "amazon api gateway": "apigateway",
    "eventbridge scheduler": "eventbridge",
    "eventbridge": "eventbridge",
    "client": "users",
    "clients": "users",
}

# Below this, two labels are different boxes rather than one box read twice.
# 0.75 accepts "goldmlops-daily-drift-check" read as "...diff-check" while
# keeping "ingest" and "monitor" apart.
LABEL_MATCH = 0.75


def normalise_service(service: str) -> str:
    key = service.strip().lower()
    return ALIASES.get(key, key)


def normalise_label(label: str) -> str:
    """Strip what differs between a drawing and a reading of it."""
    return re.sub(r"[^a-z0-9]", "", label.lower())


def label_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalise_label(a), normalise_label(b)).ratio()


@dataclass
class Score:
    case: str

    boxes_expected: int
    boxes_matched: int
    boxes_spurious: int

    services_matched: int

    edges_expected: int
    edges_correct: int
    edges_hallucinated: int

    unreadable_reported: int
    unmatched_labels: list[str]

    @property
    def box_recall(self) -> float:
        """Did it see that something is there."""
        return self.boxes_matched / self.boxes_expected if self.boxes_expected else 1.0

    @property
    def service_recall(self) -> float:
        """Of the boxes it found, did it know what they are."""
        return self.services_matched / self.boxes_expected if self.boxes_expected else 1.0

    @property
    def edge_recall(self) -> float:
        return self.edges_correct / self.edges_expected if self.edges_expected else 1.0

    @property
    def hallucinated_edge_rate(self) -> float:
        """Share of reported edges that are not in the diagram.

        The headline number: this is the failure the spike exposed and the
        reason a human confirms the graph before any review runs.
        """
        reported = self.edges_correct + self.edges_hallucinated
        return self.edges_hallucinated / reported if reported else 0.0

    def line(self) -> str:
        return (
            f"{self.case:<20} "
            f"boxes {self.boxes_matched}/{self.boxes_expected}"
            f"{f' (+{self.boxes_spurious})' if self.boxes_spurious else ''}  "
            f"services {self.services_matched}/{self.boxes_expected}  "
            f"edges {self.edges_correct}/{self.edges_expected}  "
            f"halluc {self.hallucinated_edge_rate:.0%}"
        )


def _match_boxes(truth_nodes: list[dict], graph: Graph) -> dict[str, str]:
    """Map extracted node ids to truth ids, greedily by label similarity.

    Greedy rather than optimal: the alternative is an assignment problem, and
    at ten boxes with distinct names the difference does not arise.
    """
    pairs = sorted(
        (
            (label_similarity(node.label, truth["label"]), node.id, truth["id"])
            for node in graph.nodes
            for truth in truth_nodes
        ),
        reverse=True,
    )

    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for similarity, node_id, truth_id in pairs:
        if similarity < LABEL_MATCH:
            break
        if node_id in mapping or truth_id in taken:
            continue
        mapping[node_id] = truth_id
        taken.add(truth_id)

    return mapping


def score(case_dir: Path, graph: Graph) -> Score:
    truth = json.loads((case_dir / "truth.json").read_text())
    truth_nodes = truth["nodes"]
    by_truth_id = {n["id"]: n for n in truth_nodes}

    mapping = _match_boxes(truth_nodes, graph)

    services_matched = sum(
        1
        for node in graph.nodes
        if node.id in mapping
        and normalise_service(node.service)
        == normalise_service(by_truth_id[mapping[node.id]]["service"])
    )

    truth_edges = {(e["from"], e["to"]) for e in truth["edges"]}
    found_edges = {
        (mapping[e.source], mapping[e.target])
        for e in graph.edges
        if e.source in mapping and e.target in mapping
    }
    # An edge touching a box that matched nothing cannot be checked against the
    # truth, but it is still a claim the model made — count it as hallucinated
    # rather than quietly dropping it.
    unverifiable = sum(
        1 for e in graph.edges if e.source not in mapping or e.target not in mapping
    )

    return Score(
        case=case_dir.name,
        boxes_expected=len(truth_nodes),
        boxes_matched=len(mapping),
        boxes_spurious=len(graph.nodes) - len(mapping),
        services_matched=services_matched,
        edges_expected=len(truth_edges),
        edges_correct=len(truth_edges & found_edges),
        edges_hallucinated=len(found_edges - truth_edges) + unverifiable,
        unreadable_reported=len(graph.unreadable),
        unmatched_labels=sorted(n.label for n in graph.nodes if n.id not in mapping),
    )
