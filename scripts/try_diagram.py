"""Run one diagram through the pipeline and show every step.

Built for looking at the system rather than using it: each stage prints what it
produced before the next one starts, so a surprising finding can be traced back
to the passage that caused it.

    python scripts/try_diagram.py path/to/diagram.png
    python scripts/try_diagram.py path/to/diagram.png --pillar security
    python scripts/try_diagram.py path/to/diagram.png --no-verify
    python scripts/try_diagram.py path/to/diagram.png --show-passages
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from backend.agents.extraction import extract_edges, extract_nodes
from backend.agents.supervisor import PILLARS, review
from backend.core.aws import usage
from backend.core.models import Graph, Pillar
from backend.tools.retrieval import search_pillars

BAR = "─" * 72


def heading(text: str) -> None:
    print(f"\n{BAR}\n{text}\n{BAR}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--pillar", choices=[str(p) for p in PILLARS], action="append",
                        help="limit the review to these pillars (repeatable)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the verification pass, to see what it removes")
    parser.add_argument("--show-passages", action="store_true",
                        help="print the guidance each specialist retrieved")
    args = parser.parse_args()

    if not args.image.exists():
        raise SystemExit(f"No such file: {args.image}")

    suffix = args.image.suffix.lstrip(".").lower()
    image_format = {"jpg": "jpeg"}.get(suffix, suffix)
    image = args.image.read_bytes()
    usage.reset()

    # ---- Step 1: boxes -----------------------------------------------------
    heading(f"STEP 1  Reading the boxes — {args.image.name} ({len(image) / 1e6:.1f} MB)")
    started = time.perf_counter()
    nodes, unreadable = extract_nodes(image, image_format)
    print(f"{len(nodes)} services, {time.perf_counter() - started:.1f}s\n")
    for node in nodes:
        where = f"  [in {node.group}]" if node.group else ""
        print(f"  {node.id:<28} {node.service:<22} {node.label[:48]}{where}")
    if unreadable:
        print(f"\n  flagged as unclear: {unreadable}")
    else:
        print("\n  nothing flagged as unclear — the model rarely admits doubt here")

    # ---- Step 2: arrows ----------------------------------------------------
    heading("STEP 2  Tracing the arrows — the step that invents things")
    started = time.perf_counter()
    edges = extract_edges(image, nodes, image_format)
    print(f"{len(edges)} connections, {time.perf_counter() - started:.1f}s\n")
    for edge in edges:
        label = f"  ({edge.label})" if edge.label else ""
        print(f"  {edge.source:<28} -> {edge.target}{label}")

    graph = Graph(nodes=nodes, edges=edges, unreadable=unreadable)

    print(f"\n  >> Compare this against the diagram. Every wrong arrow here is what")
    print(f"     the human gate exists to catch. Measured rate: about half.")

    # ---- Step 3: retrieval -------------------------------------------------
    pillars = [Pillar(p) for p in args.pillar] if args.pillar else PILLARS

    if args.show_passages:
        heading("STEP 3  What each specialist retrieved")
        for pillar in pillars:
            passages = search_pillars(graph.as_text(), pillar)
            print(f"\n  {str(pillar).upper()} — {len(passages)} passages above the relevance floor")
            for passage in passages:
                print(f"    d={passage.distance:.3f}  {passage.cite()}")
            if not passages:
                print("    nothing relevant — this specialist will report nothing")

    # ---- Step 4: review ----------------------------------------------------
    heading(f"STEP 4  Review by {len(pillars)} specialist(s)"
            + ("  [verification OFF]" if args.no_verify else ""))
    started = time.perf_counter()
    result = review(graph, pillars=pillars, verify_findings=not args.no_verify)
    elapsed = time.perf_counter() - started

    print(f"{len(result.findings)} findings, {len(result.rejected)} rejected, {elapsed:.1f}s\n")

    for finding in result.findings:
        source = finding.citation
        print(f"  [{finding.severity.upper():<6}] {finding.pillar}")
        print(f"    {finding.issue}")
        if finding.evidence:
            print(f"    shown by: {', '.join(finding.evidence)}")
        if finding.recommendation:
            print(f"    fix:      {finding.recommendation}")
        print(f"    source:   {source.bp_id} \"{source.bp_title}\" p.{source.page}"
              if source else "    source:   none — reported without a citation")
        print()

    if result.rejected:
        print("  REJECTED by verification — claims the diagram does not support:")
        for finding in result.rejected:
            print(f"    [{finding.pillar}] {finding.issue}")
        print()

    if result.pillars_with_nothing_to_say:
        silent = ", ".join(str(p) for p in result.pillars_with_nothing_to_say)
        print(f"  Retrieved nothing relevant: {silent}")
        print("  Those specialists stayed silent rather than guessing.\n")

    # ---- Cost --------------------------------------------------------------
    heading("WHAT IT COST")
    summary = usage.summary()
    print(f"  {summary['chat_calls']} model calls, {summary['embed_calls']} embedding calls")
    print(f"  {summary['input_tokens']:,} in / {summary['output_tokens']:,} out tokens")
    print(f"  ${summary['usd']:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
