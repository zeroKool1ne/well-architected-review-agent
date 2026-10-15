"""A verification pass over proposed findings.

Instructing a reviewer not to infer absent precautions does not work. Measured
on 14.10.2026: with the instruction in the system prompt, the deliberately
clean test diagram still drew four findings, every one of them an inference
from silence — "not secured with mTLS", "single availability zone" on a diagram
that names no zone, "no CloudFront". Telling a model to be careful competes
with its pull toward being useful, and usefulness wins.

So the check is moved out of the prompt that produces findings and into a
separate call that only judges. The judging call cannot propose anything; its
sole output is keep or drop per finding. Separating the two means the incentive
to find something no longer touches the decision about what survives.

Two rules, both answerable from the graph text alone:

  1. The claim must rest on something the diagram states, not on its silence.
  2. The evidence must name services that are actually in the graph.

Rule 2 is enforced in code, because it needs no judgement.
"""

from __future__ import annotations

from backend.agents.extraction import _items, _parse_json
from backend.core.aws import converse
from backend.core.models import Finding, Graph

SYSTEM = (
    "You check whether a claim about an architecture is supported by the "
    "description of that architecture. You do not review architectures and you "
    "do not suggest improvements. You decide supported or unsupported, nothing else."
)

PROMPT = """This is the complete description of an architecture. Treat it as all \
that is known — it is not an excerpt.

{graph}

A reviewer made these claims about it. For each, decide whether the description \
supports it.

{claims}

Supported means the fact the claim rests on appears in the description. A claim \
that restates a label, or draws the obvious conclusion from one, is supported:

- label "single-AZ" supports a claim about one availability zone
- label "public read enabled" supports a claim about public access
- label "no automated backups" supports a claim about missing backups
- label "running 24/7, ~8% CPU" supports a claim about an idle or oversized \
instance, because that is what those numbers mean

Judge the fact, not the wording. The reviewer may use different words than the \
label as long as the label is what makes the claim true.

Unsupported means the claim fills in something the description does not say. \
If the description never mentions encryption, a claim that traffic is \
unencrypted is unsupported — silence is not evidence. The same goes for claims \
about missing backups, missing redundancy or missing monitoring when the \
description simply does not cover them.

Be strict. A claim that is plausible but not stated is unsupported.

Return ONLY JSON, one entry per claim, in the same order:
{{"verdicts": [{{"n": 1, "supported": true, "why": "label states ..."}}]}}"""


def verify(graph: Graph, findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split findings into supported and rejected."""
    if not findings:
        return [], []

    known = {n.id for n in graph.nodes}

    # Rule 2 needs no model: a finding whose evidence names nothing real cannot
    # be checked against the diagram and is dropped before the call.
    checkable: list[Finding] = []
    rejected: list[Finding] = []
    for finding in findings:
        if finding.evidence and not any(e in known for e in finding.evidence):
            rejected.append(finding)
        else:
            checkable.append(finding)

    if not checkable:
        return [], rejected

    claims = "\n".join(
        f"{i}. [{f.pillar}] {f.issue}" for i, f in enumerate(checkable, start=1)
    )

    data = _parse_json(
        converse(
            PROMPT.format(graph=graph.as_text(), claims=claims),
            system=SYSTEM,
            max_tokens=1500,
        )
    )

    verdicts = {
        int(v.get("n", 0)): bool(v.get("supported"))
        for v in _items(data, "verdicts")
        if isinstance(v, dict)
    }

    kept: list[Finding] = []
    for index, finding in enumerate(checkable, start=1):
        # Default to dropping: a finding the verifier did not rule on has not
        # been checked, and unchecked claims are what this pass exists to stop.
        if verdicts.get(index, False):
            kept.append(finding)
        else:
            rejected.append(finding)

    return kept, rejected
