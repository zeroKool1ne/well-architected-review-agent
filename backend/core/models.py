"""The shapes that travel between the steps.

The graph is the contract between phase one and phase two: extraction produces
it, a human corrects it, the pillar specialists consume it. Keeping it in one
place means the review cannot quietly start reading a different structure than
extraction writes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Pillar(StrEnum):
    SECURITY = "security"
    COST = "cost"
    RELIABILITY = "reliability"


class Node(BaseModel):
    """One box in the diagram."""

    id: str = Field(description="short slug, unique within the graph")
    service: str = Field(description="AWS service, e.g. 'S3' or 'EC2'")
    label: str = Field(description="the text printed on the box, verbatim")
    group: str | None = Field(
        default=None,
        description="enclosing box if any, e.g. an availability zone or VPC",
    )


class Edge(BaseModel):
    """One arrow between two boxes."""

    source: str = Field(description="id of the node the arrow starts at")
    target: str = Field(description="id of the node the arrow points to")
    label: str | None = None


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    unreadable: list[str] = Field(
        default_factory=list,
        description="anything the model could not read with confidence",
    )

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def as_text(self) -> str:
        """Flatten to the form the reviewing agents read.

        Node labels are kept verbatim because that is where the reviewable
        detail sits: "single-AZ, no automated backups" is the finding, and
        summarising it away would hide it from the specialists.
        """
        lines = ["Services:"]
        for node in self.nodes:
            where = f" [in {node.group}]" if node.group else ""
            lines.append(f"- {node.id} ({node.service}){where}: {node.label}")

        if self.edges:
            lines.append("\nConnections:")
            for edge in self.edges:
                via = f" ({edge.label})" if edge.label else ""
                lines.append(f"- {edge.source} -> {edge.target}{via}")

        return "\n".join(lines)


class Citation(BaseModel):
    bp_id: str
    bp_title: str
    page: int
    pillar: Pillar


class Finding(BaseModel):
    pillar: Pillar
    issue: str = Field(description="what is wrong, in one or two sentences")
    evidence: list[str] = Field(
        default_factory=list,
        description="node ids that make the problem visible",
    )
    recommendation: str = ""
    citation: Citation | None = None
    severity: str = Field(default="medium", description="high | medium | low")


class ReviewResult(BaseModel):
    graph: Graph
    findings: list[Finding] = Field(default_factory=list)
    rejected: list[Finding] = Field(
        default_factory=list,
        description="findings the verification pass could not support from the graph",
    )
    pillars_with_nothing_to_say: list[Pillar] = Field(
        default_factory=list,
        description="specialists whose retrieval stayed below the relevance threshold",
    )
