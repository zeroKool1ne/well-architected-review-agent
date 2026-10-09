"""Generate test diagrams together with their ground truth.

The picture and the truth file come from one declaration, so they cannot drift
apart. That is the whole point: extraction accuracy is only measurable against
a graph we already know, and hand-labelling screenshots would be guesswork
dressed up as data.

Each case writes:
    <slug>/diagram.png   the image fed to the extraction agent
    <slug>/truth.json    nodes, edges and the findings a correct review returns

Requires graphviz (brew install graphviz).

    python evaluation/datasets/generate.py
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EC2, Lambda
from diagrams.aws.database import RDS, Dynamodb
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import ELB, APIGateway
from diagrams.aws.storage import S3
from diagrams.onprem.client import Users

HERE = Path(__file__).resolve().parent

# diagrams needs a class per node; the truth file needs a plain service name.
# Keeping both in one table stops them disagreeing.
NODE_TYPES = {
    "users": (Users, "Users"),
    "apigw": (APIGateway, "API Gateway"),
    "alb": (ELB, "Application Load Balancer"),
    "lambda": (Lambda, "Lambda"),
    "ec2": (EC2, "EC2"),
    "rds": (RDS, "RDS"),
    "dynamodb": (Dynamodb, "DynamoDB"),
    "s3": (S3, "S3"),
    "cloudwatch": (Cloudwatch, "CloudWatch"),
}


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    label: str
    cluster: str | None = None


@dataclass(frozen=True)
class Link:
    src: str
    dst: str
    label: str = ""


@dataclass(frozen=True)
class Finding:
    """A problem a correct review should report.

    `evidence` names the nodes that make the problem visible, so a finding can
    be scored on whether the agent pointed at the right part of the diagram
    rather than merely mentioning the right pillar.
    """

    pillar: str
    issue: str
    evidence: list[str]


@dataclass(frozen=True)
class Case:
    slug: str
    title: str
    purpose: str
    nodes: list[Node]
    links: list[Link]
    findings: list[Finding] = field(default_factory=list)


GRAPH_ATTR = {"fontsize": "15", "bgcolor": "white", "pad": "0.4", "splines": "spline"}


# --------------------------------------------------------------------------
# Case 1 — a clean, small pipeline.
#
# Nothing obviously wrong, which is the point: it measures whether the agent
# can read a diagram at all without being carried by glaring problems, and it
# catches a reviewer that invents findings to look useful.
# --------------------------------------------------------------------------
SIMPLE = Case(
    slug="01_simple_api",
    title="Serverless API",
    purpose="Baseline for extraction accuracy. No deliberate violations.",
    nodes=[
        Node("users", "users", "Clients"),
        Node("apigw", "apigw", "API Gateway\n(REST, TLS)"),
        Node("fn", "lambda", "Lambda\nrequest handler"),
        Node("table", "dynamodb", "DynamoDB\n(encrypted, PITR on)"),
        Node("logs", "cloudwatch", "CloudWatch Logs\n(30 day retention)"),
    ],
    links=[
        Link("users", "apigw", "HTTPS"),
        Link("apigw", "fn", "invoke"),
        Link("fn", "table", "read/write"),
        Link("fn", "logs", "logs"),
    ],
    findings=[],
)


# --------------------------------------------------------------------------
# Case 2 — deliberate violations, one per pillar.
#
# The violations are written into the node labels because a vision model can
# only report what the picture states. Real diagrams annotate this way too:
# "single AZ", "public" and "on-demand, 24/7" are the kind of notes that end up
# on a slide.
# --------------------------------------------------------------------------
FLAWED = Case(
    slug="02_flawed_webapp",
    title="Web application with known problems",
    purpose="One violation per pillar, each stated on the diagram so a vision model can see it.",
    nodes=[
        Node("users", "users", "Clients"),
        Node("alb", "alb", "Application Load Balancer", cluster="eu-central-1a"),
        Node("web", "ec2", "EC2 m5.2xlarge\non-demand, running 24/7\n~8% CPU", cluster="eu-central-1a"),
        Node("db", "rds", "RDS PostgreSQL\nsingle-AZ, no automated backups", cluster="eu-central-1a"),
        Node("assets", "s3", "S3 bucket\npublic read enabled\nno default encryption"),
        Node("logs", "cloudwatch", "CloudWatch Logs"),
    ],
    links=[
        Link("users", "alb", "HTTPS"),
        Link("alb", "web", "HTTP"),
        Link("web", "db", "SQL"),
        Link("web", "assets", "read/write"),
        Link("web", "logs", "logs"),
    ],
    findings=[
        Finding(
            pillar="security",
            issue="S3 bucket allows public read access and has no default encryption",
            evidence=["assets"],
        ),
        Finding(
            pillar="reliability",
            issue="Every compute and data component sits in a single availability zone, so the zone is a single point of failure",
            evidence=["alb", "web", "db"],
        ),
        Finding(
            pillar="reliability",
            issue="RDS instance has no automated backups, so there is no recovery point",
            evidence=["db"],
        ),
        Finding(
            pillar="cost",
            issue="Oversized on-demand instance running continuously at low utilisation",
            evidence=["web"],
        ),
    ],
)

CASES = [SIMPLE, FLAWED]


def render(case: Case) -> None:
    """Draw the diagram. diagrams writes <filename>.png next to the cwd."""
    target = HERE / case.slug
    target.mkdir(parents=True, exist_ok=True)

    clusters = [n.cluster for n in case.nodes if n.cluster]
    ordered = list(dict.fromkeys(clusters))

    handles: dict[str, object] = {}
    outfile = target / "diagram"

    with Diagram(
        case.title,
        filename=str(outfile),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=GRAPH_ATTR,
    ):
        for node in (n for n in case.nodes if not n.cluster):
            cls, _ = NODE_TYPES[node.kind]
            handles[node.id] = cls(node.label)

        for name in ordered:
            with Cluster(name):
                for node in (n for n in case.nodes if n.cluster == name):
                    cls, _ = NODE_TYPES[node.kind]
                    handles[node.id] = cls(node.label)

        for link in case.links:
            handles[link.src] >> Edge(label=link.label) >> handles[link.dst]


def truth(case: Case) -> dict:
    return {
        "slug": case.slug,
        "title": case.title,
        "purpose": case.purpose,
        "nodes": [
            {
                "id": n.id,
                "service": NODE_TYPES[n.kind][1],
                "label": n.label.replace("\n", " "),
                **({"cluster": n.cluster} if n.cluster else {}),
            }
            for n in case.nodes
        ],
        "edges": [
            {"from": link.src, "to": link.dst, **({"label": link.label} if link.label else {})}
            for link in case.links
        ],
        "expected_findings": [
            {"pillar": f.pillar, "issue": f.issue, "evidence": f.evidence}
            for f in case.findings
        ],
    }


def main() -> int:
    if shutil.which("dot") is None:
        raise SystemExit("graphviz not found. Install it with: brew install graphviz")

    for case in CASES:
        render(case)
        path = HERE / case.slug / "truth.json"
        path.write_text(json.dumps(truth(case), indent=2) + "\n")

        print(
            f"  {case.slug:<20} {len(case.nodes)} nodes, {len(case.links)} edges, "
            f"{len(case.findings)} expected findings"
        )

    print(f"\nWrote {len(CASES)} cases to {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
