"""The extraction agent: image in, graph out.

Deliberately two passes rather than one. The spike on 06.10.2026 asked a single
prompt for services, connections and confidence together, and the result split
cleanly: all 21 box labels of a real diagram were read correctly, while the
connections were invented — the boxes were chained into a sequence that does
not exist in the picture — and confidence came back at 0.95 throughout.

Reading labels and tracing arrows are different tasks, and asking for both at
once lets the easy one carry the hard one. So:

  pass 1  boxes only, where the model is reliable
  pass 2  arrows only, given the known boxes, with declining to answer made
          the explicit easy path

Whatever survives still goes to a human before any review runs. This narrows
the gap rather than closing it.
"""

from __future__ import annotations

import json
import re

from backend.core.aws import converse
from backend.core.models import Edge, Graph, Node

SYSTEM = (
    "You read AWS architecture diagrams. You report only what is visibly present. "
    "Inventing a plausible detail is a failure, omitting an unclear one is correct."
)

NODES_PROMPT = """List every box in this AWS architecture diagram.

For each box return:
  id      a short lowercase slug you invent, unique in this diagram
  service the AWS service it depicts, e.g. "S3", "EC2", "Lambda", "RDS".
          Use "Users" for client/actor icons. If the icon is unclear, use "Unknown".
  label   the text printed on or under the box, copied verbatim, including
          any annotations such as "single-AZ" or "public read enabled".
          Join multiple lines with a single space.
  group   the name of the enclosing box if the box sits inside one
          (an availability zone, a VPC, a region), otherwise null

A box always has a service icon. Do NOT list as a box:
- the diagram's own title or caption, which sits outside the drawing
- text sitting on an arrow, such as "HTTPS", "logs", "SQL" or "read/write" —
  that is a connection label and belongs to the arrow, not to a box

Also return `unreadable`: a list of short descriptions of anything you cannot
read with confidence. An empty list is a strong claim — only make it if the
diagram really is fully legible.

Return ONLY JSON:
{"nodes": [{"id": "...", "service": "...", "label": "...", "group": null}],
 "unreadable": []}"""

EDGES_TEMPLATE = """These boxes were identified in the diagram:

{inventory}

Now trace the ARROWS between them, and only the arrows.

Rules:
- Report an arrow only if you can see a line or arrowhead connecting two boxes.
- Do not infer a connection because two boxes are adjacent, aligned, or because
  the architecture would normally work that way.
- Boxes being listed in order above says nothing about how they connect.
- Several arrows may leave the same box. A box that feeds three others is
  normal; do not turn that into a chain through them.
- Follow each line from its start to its arrowhead before reporting it. If a
  line passes near a box without touching it, it does not connect to that box.
- If you cannot make out the arrows, return an empty list. That is a correct
  and expected answer, not a failure.

For each arrow return `source` and `target` as ids from the list above, and
`label` as the text printed on the arrow, or null.

Return ONLY JSON:
{{"edges": [{{"source": "...", "target": "...", "label": null}}]}}"""


def _parse_json(raw: str) -> dict | list:
    """Pull the JSON out of a model reply.

    Models wrap JSON in prose or fences often enough that failing on it would be
    a routine outage rather than an exception. They also drop the requested
    envelope and return the bare array, so both shapes are recovered and callers
    normalise what they get.
    """
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"No JSON in model reply: {raw[:200]}")


def _items(parsed: dict | list, key: str) -> list:
    """Read a list the model was asked to nest under `key`, however it arrived."""
    if isinstance(parsed, list):
        return parsed
    value = parsed.get(key, [])
    return value if isinstance(value, list) else []


def extract_nodes(image: bytes, image_format: str = "png") -> tuple[list[Node], list[str]]:
    data = _parse_json(
        converse(NODES_PROMPT, system=SYSTEM, image=image, image_format=image_format)
    )

    nodes: list[Node] = []
    seen: set[str] = set()
    for item in _items(data, "nodes"):
        node = Node(
            id=str(item.get("id", "")).strip(),
            service=str(item.get("service", "Unknown")).strip(),
            label=" ".join(str(item.get("label", "")).split()),
            group=(item.get("group") or None),
        )
        # A duplicate id would silently merge two boxes into one.
        if node.id and node.id not in seen:
            seen.add(node.id)
            nodes.append(node)

    unreadable = data.get("unreadable", []) if isinstance(data, dict) else []
    return nodes, [str(u) for u in unreadable]


def extract_edges(
    image: bytes, nodes: list[Node], image_format: str = "png"
) -> list[Edge]:
    if len(nodes) < 2:
        return []

    inventory = "\n".join(f"- {n.id}: {n.service} — {n.label}" for n in nodes)
    data = _parse_json(
        converse(
            EDGES_TEMPLATE.format(inventory=inventory),
            system=SYSTEM,
            image=image,
            image_format=image_format,
        )
    )

    known = {n.id for n in nodes}
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for item in _items(data, "edges"):
        source, target = str(item.get("source", "")), str(item.get("target", ""))
        # An edge naming a box that does not exist is a hallucination with a
        # cheap tell — drop it here rather than asking the human about it.
        if source not in known or target not in known or source == target:
            continue
        if (source, target) in seen:
            continue
        seen.add((source, target))
        edges.append(Edge(source=source, target=target, label=item.get("label") or None))

    return edges


def extract_graph(image: bytes, image_format: str = "png") -> Graph:
    nodes, unreadable = extract_nodes(image, image_format)
    edges = extract_edges(image, nodes, image_format)
    return Graph(nodes=nodes, edges=edges, unreadable=unreadable)
