"""Score an extracted graph against the ground truth of a generated test case.

Matching is by service rather than by id, because the model invents its own ids
and there is no reason they would agree with ours. Services are normalised
first: a model that answers "ELB" where the truth file says "Application Load
Balancer" is right, and scoring it wrong would measure vocabulary instead of
comprehension.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    "client": "users",
    "clients": "users",
}


def normalise(service: str) -> str:
    key = service.strip().lower()
    return ALIASES.get(key, key)


@dataclass
class Score:
    case: str
    nodes_expected: int
    nodes_found: int
    nodes_matched: int
    nodes_spurious: list[str]
    edges_expected: int
    edges_correct: int
    edges_hallucinated: int
    unreadable_reported: int

    @property
    def node_recall(self) -> float:
        return self.nodes_matched / self.nodes_expected if self.nodes_expected else 1.0

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
            f"nodes {self.nodes_matched}/{self.nodes_expected} "
            f"(+{len(self.nodes_spurious)} spurious)  "
            f"edges {self.edges_correct}/{self.edges_expected} "
            f"halluc {self.hallucinated_edge_rate:.0%}"
        )


def score(case_dir: Path, graph: Graph) -> Score:
    truth = json.loads((case_dir / "truth.json").read_text())

    truth_service = {n["id"]: normalise(n["service"]) for n in truth["nodes"]}
    found_service = {n.id: normalise(n.service) for n in graph.nodes}

    expected = sorted(truth_service.values())
    found = sorted(found_service.values())

    # Multiset intersection: two S3 buckets in the truth need two in the answer.
    remaining = list(found)
    matched = 0
    for service in expected:
        if service in remaining:
            remaining.remove(service)
            matched += 1

    truth_edges = {
        (truth_service[e["from"]], truth_service[e["to"]]) for e in truth["edges"]
    }
    found_edges = {
        (found_service[e.source], found_service[e.target])
        for e in graph.edges
        if e.source in found_service and e.target in found_service
    }

    return Score(
        case=case_dir.name,
        nodes_expected=len(expected),
        nodes_found=len(found),
        nodes_matched=matched,
        nodes_spurious=sorted(remaining),
        edges_expected=len(truth_edges),
        edges_correct=len(truth_edges & found_edges),
        edges_hallucinated=len(found_edges - truth_edges),
        unreadable_reported=len(graph.unreadable),
    )
