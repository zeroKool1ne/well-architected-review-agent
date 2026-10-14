"""The pillar specialists: one factory, three instances.

Each specialist differs only in its system prompt and in the slice of the
corpus it is allowed to retrieve from. That narrowness is the point. Measured
on 07.10.2026, an unfiltered search over a multi-AZ question returns reliability
guidance exclusively and never raises the cost of cross-zone traffic; a question
about backups returns reliability and never asks whether they are encrypted.
Filtering does not produce better passages, it forces a perspective that would
otherwise never be taken.

The same measurement set the floor for staying silent: where a pillar has
nothing to say, its closest passage sits around 0.87 cosine distance. Handing
those to the model would invite it to construct a finding from unrelated text,
so the specialist reports nothing instead.
"""

from __future__ import annotations


from backend.agents.extraction import _items, _parse_json
from backend.core.aws import converse
from backend.core.models import Citation, Finding, Graph, Pillar
from backend.tools.retrieval import Passage, search_pillars

# What each specialist looks for, and the query it uses to pull its own slice of
# the corpus. The query is written in the vocabulary of the whitepaper rather
# than of the diagram, because that is what the embeddings were built from.
BRIEFS: dict[Pillar, dict[str, str]] = {
    Pillar.SECURITY: {
        "focus": (
            "public access, identity and permission scope, encryption of data at "
            "rest and in transit, secrets handling, network exposure, audit logging"
        ),
        "query": (
            "public access to storage, encryption at rest and in transit, least "
            "privilege permissions, secure network exposure, credential handling"
        ),
        "out_of_scope": "spend, instance sizing, availability zones or backups",
    },
    Pillar.COST: {
        "focus": (
            "always-on compute, oversized instances, idle or low-utilisation "
            "resources, pricing model, storage tiering, data transfer charges"
        ),
        "query": (
            "right sizing compute, on-demand versus committed pricing, idle "
            "resources, elasticity, storage class selection, data transfer cost"
        ),
        "out_of_scope": "access control, encryption, backups or fault tolerance",
    },
    Pillar.RELIABILITY: {
        "focus": (
            "single points of failure, availability zone and region spread, "
            "backups and recovery objectives, health checks, retries, decoupling"
        ),
        "query": (
            "multiple availability zones, single point of failure, automated "
            "backup and recovery, fault isolation, scaling under failure"
        ),
        "out_of_scope": "permissions, encryption or the price of resources",
    },
}

SYSTEM_TEMPLATE = """You review AWS architectures against the {pillar} pillar of the \
AWS Well-Architected Framework.

SCOPE. You look only at {focus}.

Other reviewers cover the remaining pillars and will report what you skip. \
Reporting a problem outside your scope is an error even when the problem is \
real: it duplicates their work and attaches the wrong guidance to it. If an \
issue is mainly about {out_of_scope}, it is not yours.

EVIDENCE. Report only what the diagram states. The architecture you are given \
is the complete description — anything it does not mention is unknown, not \
absent. Never write that something is missing, disabled, unencrypted or \
unprotected because the diagram is silent about it. A diagram that does not \
mention TLS is not a diagram showing unencrypted traffic.

You may report a stated fact ("labelled single-AZ", "labelled public read"). \
You may not report an inferred gap.

Reporting nothing is a valid and often correct outcome. Never stretch a passage \
to fit an architecture it does not cover."""

PROMPT_TEMPLATE = """Architecture under review:

{graph}

Relevant guidance from the {pillar} pillar:

{passages}

Report problems in this architecture that the guidance above actually covers.

For each one:
  issue           what is wrong and why it matters, two sentences at most
  evidence        the service ids from the architecture that show the problem
  recommendation  the concrete change, one sentence
  citation        the id of the guidance passage you used, e.g. "{example_bp}"
  severity        high, medium or low

Report nothing you cannot tie to one of the passages above. An empty list is a \
valid answer.

Return ONLY JSON:
{{"findings": [{{"issue": "...", "evidence": ["..."], "recommendation": "...", \
"citation": "...", "severity": "medium"}}]}}"""


def _format(passages: list[Passage]) -> str:
    return "\n\n".join(
        f"[{p.citation.bp_id}] {p.citation.bp_title} (p. {p.citation.page})\n{p.text}"
        for p in passages
    )


def review_pillar(graph: Graph, pillar: Pillar) -> tuple[list[Finding], bool]:
    """Review one pillar.

    Returns the findings and whether the specialist had anything to work with.
    The flag matters: no findings because the architecture is sound and no
    findings because retrieval came back empty are different outcomes, and
    collapsing them would hide a silent failure.
    """
    brief = BRIEFS[pillar]

    # The graph text is part of the query so retrieval reacts to this specific
    # architecture, not only to the pillar's standing concerns.
    passages = search_pillars(f"{brief['query']}\n\n{graph.as_text()}", pillar)
    if not passages:
        return [], False

    reply = converse(
        PROMPT_TEMPLATE.format(
            graph=graph.as_text(),
            pillar=pillar,
            passages=_format(passages),
            example_bp=passages[0].citation.bp_id,
        ),
        system=SYSTEM_TEMPLATE.format(
            pillar=pillar,
            focus=brief["focus"],
            out_of_scope=brief["out_of_scope"],
        ),
        max_tokens=2000,
    )

    by_id = {p.citation.bp_id: p.citation for p in passages}
    known_nodes = {n.id for n in graph.nodes}

    findings: list[Finding] = []
    for item in _items(_parse_json(reply), "findings"):
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue

        # A citation naming a passage the specialist was not given is invented;
        # dropping the citation keeps the finding but marks it as unsupported.
        citation: Citation | None = by_id.get(str(item.get("citation", "")).strip())

        findings.append(
            Finding(
                pillar=pillar,
                issue=issue,
                evidence=[e for e in item.get("evidence", []) if e in known_nodes],
                recommendation=str(item.get("recommendation", "")).strip(),
                citation=citation,
                severity=str(item.get("severity", "medium")).strip().lower(),
            )
        )

    return findings, True
