"""Build the Chroma index the pillar specialists retrieve from.

Chunking is structural first, size-based second:

1. Split at Best Practice boundaries (``SEC02-BP01`` and friends). AWS writes
   one self-contained recommendation per Best Practice, so this gives every
   chunk an identity that can be cited rather than a bare page number.
2. Split again inside a Best Practice, because the sections run to ~6,600
   characters at the median (measured across all three pillars). A chunk that
   large averages several topics into one embedding and wins no search.

Every chunk carries its pillar as metadata. That filter is what makes the
three specialist agents specialists rather than three generalists.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import boto3
import chromadb
from botocore.config import Config
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
INDEX_DIR = ROOT / "data" / "chroma"

COLLECTION = "well_architected"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
REGION = "us-east-1"

# Best Practice ids are prefixed per pillar in the AWS source documents.
PILLAR_PREFIX = {"security": "SEC", "cost": "COST", "reliability": "REL"}

# A real Best Practice section always runs longer than this; anything shorter
# means the anchor landed on page furniture rather than a heading.
MIN_SECTION_CHARS = 400

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 180

PAGE_MARKER = re.compile(r"\x00PAGE(\d+)\x00")

# Running header repeated on every page, e.g. "Security Pillar AWS Well-Architected Framework".
HEADER = re.compile(r"^.{0,60}AWS Well-Architected Framework\s*$", re.MULTILINE)
# Contents entries, recognisable by their leader dots.
TOC_LINE = re.compile(r"^.*\.{4,}.*$", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    text: str
    pillar: str
    bp_id: str
    bp_title: str
    page: int

    def chunk_id(self, ordinal: int) -> str:
        return f"{self.pillar}:{self.bp_id}:{ordinal}"


def read_pdf(path: Path) -> str:
    """Extract text, interleaving invisible page markers for later citation."""
    reader = PdfReader(path)
    pages = (
        f"\x00PAGE{number}\x00" + (page.extract_text() or "")
        for number, page in enumerate(reader.pages, start=1)
    )
    return "\n".join(pages)


def page_at(text: str, position: int) -> int:
    """Page number of the last marker at or before `position`."""
    markers = [m for m in PAGE_MARKER.finditer(text, 0, position + 1)]
    return int(markers[-1].group(1)) if markers else 1


def strip_furniture(text: str, prefix: str) -> str:
    """Remove page furniture that otherwise masquerades as section headings.

    Every page of a Best Practice repeats its id and title as a footer, with the
    printed page number appended: `SEC01-BP01 Separate workloads using accounts 10`.
    Those footers outnumber the real heading and sit *after* it, so leaving them
    in makes the last occurrence of an id land at the end of its own section —
    which silently attaches the next Best Practice's text to the previous id.

    Blanked rather than deleted, so character offsets stay aligned with the page
    markers used for citations.
    """
    footer = re.compile(rf"^{prefix}\d{{2}}-BP\d{{2}}\s+.+?\s+\d+\s*$", re.MULTILINE)

    def blank(match: re.Match[str]) -> str:
        """Blank the match but keep any page marker inside it.

        A running header sits on the same line as the marker that opens its
        page, so blanking the line wholesale would erase the page number every
        citation depends on.
        """
        span = match.group(0)
        out: list[str] = []
        cursor = 0
        for marker in PAGE_MARKER.finditer(span):
            out.append(" " * (marker.start() - cursor))
            out.append(marker.group(0))
            cursor = marker.end()
        out.append(" " * (len(span) - cursor))
        return "".join(out)

    for pattern in (footer, HEADER, TOC_LINE):
        text = pattern.sub(blank, text)
    return text


def body_start(text: str) -> int:
    """Offset where the contents section ends and the document body begins.

    Line-wise filtering cannot catch every contents entry: long titles wrap, and
    the leader dots then sit on the continuation line while the line carrying the
    id looks like an ordinary heading. Treating the contents as a *region* and
    searching only after it avoids that whole class of edge case.

    The search is capped at the first fifth of the document so that a run of dots
    appearing later in the text cannot drag the boundary into the body.

    Must be called on raw text: `strip_furniture` blanks the leader dots this
    relies on.
    """
    horizon = len(text) // 5
    hits = list(TOC_LINE.finditer(text, 0, horizon))
    return hits[-1].end() if hits else 0


def split_best_practices(text: str, prefix: str, start: int = 0) -> list[tuple[str, str, str, int]]:
    """Cut the document into (id, title, body, page) per Best Practice.

    Runs on text with page furniture already stripped, which is what makes the
    *last* occurrence of an id the real heading: the contents entry and any
    cross-reference in an earlier chapter both precede it, and the per-page
    footers that used to follow it are blanked.
    """
    pattern = re.compile(rf"^({prefix}\d{{2}}-BP\d{{2}})\s+(\S.*)$", re.MULTILINE)

    candidates: dict[str, list[re.Match[str]]] = {}
    for match in pattern.finditer(text, start):
        candidates.setdefault(match.group(1), []).append(match)

    # Start at the last candidate per id, then walk backwards for any whose body
    # comes out too short to be a real section. That happens when a footer slips
    # through the line-wise filter — a wrapped title puts the page number on the
    # following line, leaving a remainder that looks like a heading. Correcting
    # by outcome is sturdier than widening the pattern for each new variant.
    chosen = {bp: len(hits) - 1 for bp, hits in candidates.items()}

    for _ in range(4):
        anchors = sorted(
            (candidates[bp][i] for bp, i in chosen.items()), key=lambda m: m.start()
        )
        offsets = [m.start() for m in anchors] + [len(text)]

        short = {
            anchors[i].group(1)
            for i in range(len(anchors))
            if offsets[i + 1] - offsets[i] < MIN_SECTION_CHARS
        }
        movable = {bp for bp in short if chosen[bp] > 0}
        if not movable:
            break
        for bp in movable:
            chosen[bp] -= 1

    anchors = sorted(
        (candidates[bp][i] for bp, i in chosen.items()), key=lambda m: m.start()
    )

    sections: list[tuple[str, str, str, int]] = []
    for index, match in enumerate(anchors):
        body_from = match.end()
        body_to = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
        body = PAGE_MARKER.sub(" ", text[body_from:body_to])
        body = re.sub(r"[ \t]{2,}", " ", body).strip()
        if body:
            sections.append((match.group(1), match.group(2).strip(), body, page_at(text, match.start())))
    return sections


# Best Practices that genuinely never appear as a heading in the source PDF —
# only as cross-references. Listing them keeps the completeness check strict
# without failing on a defect in the upstream document.
KNOWN_ABSENT = {"REL12-BP06"}


def validate(sections: list[tuple[str, str, str, int]], pillar: str, raw: str, prefix: str) -> None:
    """Fail loudly on the failure modes that are invisible in the output.

    A mislabelled chunk does not crash anything — it quietly produces findings
    that cite the wrong Best Practice, and that only surfaces during evaluation.
    """
    problems: list[str] = []

    dirty = [bp for bp, title, _, _ in sections if re.search(r"\s\d+$", title)]
    if dirty:
        problems.append(f"{len(dirty)} titles still end in a page number, e.g. {dirty[:3]}")

    tiny = [bp for bp, _, body, _ in sections if len(body) < MIN_SECTION_CHARS]
    if tiny:
        problems.append(f"{len(tiny)} sections are suspiciously short, e.g. {tiny[:3]}")

    ids = [bp for bp, _, _, _ in sections]
    if len(ids) != len(set(ids)):
        problems.append("duplicate best practice ids")

    # Every id the document mentions should end up with a section of its own.
    # Without this a parsing change can silently drop guidance, and nothing
    # downstream would notice.
    declared = set(re.findall(rf"\b{prefix}\d{{2}}-BP\d{{2}}\b", raw))
    missing = declared - set(ids) - KNOWN_ABSENT
    if missing:
        problems.append(f"{len(missing)} best practices have no section: {sorted(missing)}")

    pages = [page for _, _, _, page in sections]
    if pages and pages.count(1) > 1:
        problems.append("page numbers collapsed to 1 — page markers were lost while stripping")

    if problems:
        raise SystemExit(f"Ingestion check failed for {pillar}:\n  - " + "\n  - ".join(problems))


def split_by_size(body: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Slide a window over the body, preferring to break at a sentence end.

    The overlap keeps a sentence that lands on a boundary from losing the
    context that preceded it.
    """
    if len(body) <= size:
        return [body]

    pieces: list[str] = []
    start = 0
    while start < len(body):
        end = min(start + size, len(body))
        if end < len(body):
            # Look for a sentence break in the last quarter of the window.
            window = body[start + (size * 3 // 4) : end]
            breakpoint = window.rfind(". ")
            if breakpoint != -1:
                end = start + (size * 3 // 4) + breakpoint + 1

        piece = body[start:end].strip()
        if piece:
            pieces.append(piece)

        if end >= len(body):
            break
        start = max(end - overlap, start + 1)

    return pieces


def build_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for pillar, prefix in PILLAR_PREFIX.items():
        path = RAW_DIR / f"{pillar}.pdf"
        if not path.exists():
            raise SystemExit(f"Missing {path}. Run backend/ingestion/fetch_corpus.py first.")

        raw = read_pdf(path)
        # The contents boundary is measured before blanking, which removes the
        # leader dots that mark it.
        text = strip_furniture(raw, prefix)
        sections = split_best_practices(text, prefix, start=body_start(raw))
        validate(sections, pillar, raw, prefix)

        before = len(chunks)
        for bp_id, bp_title, body, page in sections:
            for piece in split_by_size(body):
                chunks.append(Chunk(piece, pillar, bp_id, bp_title, page))

        print(f"  {pillar:<12} {len(sections):>3} best practices -> {len(chunks) - before:>4} chunks")

    return chunks


def embed_all(texts: list[str], batch_log_every: int = 200) -> list[list[float]]:
    """Embed every chunk. Titan takes one input per call, so this is a loop.

    boto3's adaptive retry mode handles Bedrock throttling without a hand-rolled
    backoff.
    """
    client = boto3.client(
        "bedrock-runtime",
        region_name=REGION,
        config=Config(retries={"max_attempts": 8, "mode": "adaptive"}),
    )

    vectors: list[list[float]] = []
    started = time.perf_counter()
    for index, text in enumerate(texts, start=1):
        response = client.invoke_model(
            modelId=EMBED_MODEL,
            body=json.dumps({"inputText": text}),
        )
        vectors.append(json.loads(response["body"].read())["embedding"])

        if index % batch_log_every == 0 or index == len(texts):
            rate = index / (time.perf_counter() - started)
            print(f"  embedded {index:>5}/{len(texts)}  ({rate:.1f}/s)")

    return vectors


def write_index(chunks: list[Chunk], vectors: list[list[float]]) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(INDEX_DIR))

    # Rebuild from scratch so the index always matches the current source.
    if COLLECTION in {c.name for c in client.list_collections()}:
        client.delete_collection(COLLECTION)

    collection = client.create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    seen: dict[str, int] = {}
    ids: list[str] = []
    for chunk in chunks:
        key = f"{chunk.pillar}:{chunk.bp_id}"
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        ids.append(chunk.chunk_id(ordinal))

    metadatas = [
        {k: v for k, v in asdict(chunk).items() if k != "text"} for chunk in chunks
    ]

    # Chroma caps how much lands in one call; 500 stays well inside it.
    for start in range(0, len(chunks), 500):
        stop = start + 500
        collection.add(
            ids=ids[start:stop],
            documents=[c.text for c in chunks[start:stop]],
            embeddings=vectors[start:stop],
            metadatas=metadatas[start:stop],
        )

    print(f"  wrote {collection.count()} chunks to {INDEX_DIR}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="chunk only, skip embedding and writing — useful for tuning chunk size",
    )
    args = parser.parse_args()

    print("Chunking:")
    chunks = build_chunks()

    sizes = sorted(len(c.text) for c in chunks)
    print(
        f"\n  {len(chunks)} chunks | "
        f"median {sizes[len(sizes) // 2]} chars | "
        f"min {sizes[0]} | max {sizes[-1]}"
    )

    if args.dry_run:
        print("\nDry run — nothing embedded, nothing written.")
        return 0

    print("\nEmbedding:")
    vectors = embed_all([c.text for c in chunks])

    print("\nWriting index:")
    write_index(chunks, vectors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
