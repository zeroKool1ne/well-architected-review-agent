"""The supervisor: decides what runs, then merges what comes back.

Routing is deliberately explicit rather than left to a model. The decisions here
are a state check and a fan-out, and asking a language model to make them would
add latency, cost and a failure mode in exchange for nothing — a reviewer that
sometimes forgets to run the security pillar is worse than one that always runs
all three.

Where judgement is actually needed — reading a diagram, weighing an
architecture against guidance — a model does the work. That split is the point:
models for judgement, code for control flow.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from backend.agents.review import review_pillar
from backend.agents.verify import verify
from backend.core.models import Finding, Graph, Pillar, ReviewResult

PILLARS = [Pillar.SECURITY, Pillar.COST, Pillar.RELIABILITY]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _rank(finding: Finding) -> tuple[int, int, str]:
    """Most severe first, then findings backed by a citation, then stable."""
    return (
        SEVERITY_ORDER.get(finding.severity, 1),
        0 if finding.citation else 1,
        finding.issue,
    )


STOPWORDS = frozenset(
    "a an and are as at be by for from has in is its no not of on or the to with "
    "without this that enabled used using".split()
)


def _topic(finding: Finding) -> frozenset[str]:
    """The words that carry the meaning of a finding, for comparison.

    Two-character words are kept: "s3", "az" and "ec2" are exactly the tokens
    that distinguish one finding from another here, and dropping them as noise
    made two reports about the same bucket look unrelated.
    """
    words = re.findall(r"[a-z0-9]+", finding.issue.lower())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 1)


def _same_problem(a: Finding, b: Finding) -> bool:
    """Whether two findings are about the same thing.

    Shared evidence alone is not enough — one service can have two unrelated
    problems, and an oversized instance is not the same finding as an instance
    in a single zone. Shared wording alone is not enough either, since two
    services can have the same kind of problem. Both together are a duplicate.
    """
    if not (set(a.evidence) & set(b.evidence)):
        return False

    topic_a, topic_b = _topic(a), _topic(b)
    if not topic_a or not topic_b:
        return False

    overlap = len(topic_a & topic_b) / min(len(topic_a), len(topic_b))
    return overlap >= 0.5


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Drop findings that two specialists raised about the same thing.

    Specialists overlap by design: a publicly readable bucket is a security
    problem, and the cost reviewer may reach it through data transfer. Reporting
    it twice makes the output look padded, so the higher-ranked one wins.

    Findings without evidence are kept — they cannot be matched to anything, and
    silently dropping an unmatched finding would hide it rather than merge it.
    """
    kept: list[Finding] = []

    for finding in sorted(findings, key=_rank):
        if any(_same_problem(finding, existing) for existing in kept):
            continue
        kept.append(finding)

    return kept


def review(
    graph: Graph,
    pillars: list[Pillar] | None = None,
    *,
    verify_findings: bool = True,
) -> ReviewResult:
    """Run the specialists over a confirmed graph and merge what survives.

    The specialists are independent, so they run concurrently. Three sequential
    reviews would take three times as long for no benefit, and latency is one of
    the things this project is measured on.

    Findings then pass a separate verification call before they are returned.
    `verify_findings=False` skips it, which is how the evaluation measures what
    the step is worth.
    """
    targets = pillars or PILLARS

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = list(pool.map(lambda p: (p, *review_pillar(graph, p)), targets))

    findings: list[Finding] = []
    silent: list[Pillar] = []
    for pillar, pillar_findings, had_guidance in results:
        findings.extend(pillar_findings)
        if not had_guidance:
            silent.append(pillar)

    findings = _deduplicate(findings)

    rejected: list[Finding] = []
    if verify_findings:
        findings, rejected = verify(graph, findings)

    return ReviewResult(
        graph=graph,
        findings=findings,
        rejected=rejected,
        pillars_with_nothing_to_say=silent,
    )
