"""The two baselines the full pipeline is measured against.

Neither is a straw man. Each removes exactly one design decision and keeps
everything else, so the difference is attributable.

  B1  no verification pass. Isolates what the separate judging call is worth.
  B2  one generalist reviewer searching the whole corpus, instead of three
      specialists each confined to one pillar. Isolates what the fan-out buys.

B2 is the honest version of "why not just one agent", which is the first
question anyone asks about a multi-agent design.
"""

from __future__ import annotations

import numpy as np

from backend.agents.extraction import _items, _parse_json
from backend.agents.review import _format
from backend.agents.verify import verify
from backend.core.aws import converse, embed
from backend.core.config import settings
from backend.core.models import Citation, Finding, Graph, Pillar, ReviewResult
from backend.tools.retrieval import Passage, _array_index

SYSTEM = """You review AWS architectures against the AWS Well-Architected \
Framework, covering security, cost optimisation and reliability.

You judge only what the diagram states. The architecture you are given is the \
complete description — anything it does not mention is unknown, not absent.

Reporting nothing is a valid outcome."""

PROMPT = """Architecture under review:

{graph}

Relevant guidance from the Well-Architected Framework:

{passages}

Report problems in this architecture that the guidance above actually covers.

For each one:
  pillar          security, cost or reliability
  issue           what is wrong and why it matters, two sentences at most
  evidence        the service ids from the architecture that show the problem
  recommendation  the concrete change, one sentence
  citation        the id of the guidance passage you used
  severity        high, medium or low

Return ONLY JSON:
{{"findings": [{{"pillar": "...", "issue": "...", "evidence": ["..."], \
"recommendation": "...", "citation": "...", "severity": "medium"}}]}}"""

# The generalist gets as many passages as the three specialists get between
# them, so the comparison is about where retrieval looks rather than how much
# it returns.
GENERALIST_LIMIT = 12


def search_everywhere(query: str, limit: int = GENERALIST_LIMIT) -> list[Passage]:
    """Retrieve across all pillars at once — what a single agent would see."""
    vectors, chunks = _array_index()

    query_vector = np.asarray(embed(query), dtype=np.float32)
    query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)

    similarity = vectors @ query_vector
    top = np.argsort(-similarity)[:limit]

    passages: list[Passage] = []
    for i in top:
        distance = float(1.0 - similarity[i])
        if distance > settings.relevance_threshold:
            continue
        meta = chunks[i]
        passages.append(
            Passage(
                text=meta["text"],
                distance=distance,
                citation=Citation(
                    bp_id=meta["bp_id"],
                    bp_title=meta["bp_title"],
                    page=int(meta["page"]),
                    pillar=Pillar(meta["pillar"]),
                ),
            )
        )
    return passages


def review_generalist(graph: Graph, *, verify_findings: bool = True) -> ReviewResult:
    """Baseline 2: one reviewer, one unfiltered search, all three pillars."""
    passages = search_everywhere(graph.as_text())
    if not passages:
        return ReviewResult(graph=graph, findings=[], rejected=[])

    reply = converse(
        PROMPT.format(graph=graph.as_text(), passages=_format(passages)),
        system=SYSTEM,
        max_tokens=2500,
    )

    by_id = {p.citation.bp_id: p.citation for p in passages}
    known = {n.id for n in graph.nodes}

    findings: list[Finding] = []
    for item in _items(_parse_json(reply), "findings"):
        issue = str(item.get("issue", "")).strip()
        if not issue:
            continue
        try:
            pillar = Pillar(str(item.get("pillar", "")).strip().lower())
        except ValueError:
            continue

        findings.append(
            Finding(
                pillar=pillar,
                issue=issue,
                evidence=[e for e in item.get("evidence", []) if e in known],
                recommendation=str(item.get("recommendation", "")).strip(),
                citation=by_id.get(str(item.get("citation", "")).strip()),
                severity=str(item.get("severity", "medium")).strip().lower(),
            )
        )

    rejected: list[Finding] = []
    if verify_findings:
        findings, rejected = verify(graph, findings)

    return ReviewResult(graph=graph, findings=findings, rejected=rejected)


def pillar_coverage(result: ReviewResult) -> set[Pillar]:
    """Which pillars the review actually spoke about.

    The measured claim for the fan-out is that it forces perspectives a single
    search never reaches, so coverage is the number that tests it.
    """
    return {f.pillar for f in result.findings}
