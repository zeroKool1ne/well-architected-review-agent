"""Does the pillar metadata filter actually change what comes back?

The three specialist agents are only specialists if filtering by pillar
retrieves different passages than searching the whole corpus. If an unfiltered
search already returns the right pillar's content, the multi-agent split buys
nothing and the design should be reconsidered.

This probe makes that difference visible before the agents are built, and the
same comparison becomes baseline 2 in the evaluation.

    python experiments/retrieval_probe.py
    python experiments/retrieval_probe.py "how do I encrypt data at rest"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3
import chromadb

ROOT = Path(__file__).resolve().parents[1]
INDEX_DIR = ROOT / "data" / "chroma"
COLLECTION = "well_architected"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"

# Questions a reviewer would actually ask, one per pillar.
DEFAULT_QUERIES = [
    ("security", "an S3 bucket is publicly readable"),
    ("cost", "an EC2 instance runs around the clock at low utilisation"),
    ("reliability", "the workload runs in a single availability zone"),
]

TOP_K = 3


def embed(text: str) -> list[float]:
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    response = client.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text}),
    )
    return json.loads(response["body"].read())["embedding"]


def search(collection, vector: list[float], pillar: str | None) -> list[dict]:
    result = collection.query(
        query_embeddings=[vector],
        n_results=TOP_K,
        where={"pillar": pillar} if pillar else None,
    )
    return [
        {"distance": d, **m}
        for d, m in zip(result["distances"][0], result["metadatas"][0])
    ]


def show(label: str, hits: list[dict]) -> None:
    print(f"  {label}")
    for hit in hits:
        print(
            f"    [{hit['pillar']:<11}] {hit['bp_id']:<11} "
            f"d={hit['distance']:.3f}  {hit['bp_title'][:58]}"
        )


def main() -> int:
    if not INDEX_DIR.exists():
        raise SystemExit("No index. Run backend/ingestion/build_index.py first.")

    collection = chromadb.PersistentClient(path=str(INDEX_DIR)).get_collection(COLLECTION)
    print(f"Index: {collection.count()} chunks\n")

    queries = (
        [(None, " ".join(sys.argv[1:]))] if len(sys.argv) > 1 else DEFAULT_QUERIES
    )

    off_pillar_total = 0
    for expected_pillar, question in queries:
        vector = embed(question)
        print(f'"{question}"')

        unfiltered = search(collection, vector, None)
        show("unfiltered (what a generalist agent would see)", unfiltered)

        if expected_pillar:
            filtered = search(collection, vector, expected_pillar)
            show(f"filtered to {expected_pillar} (what the specialist sees)", filtered)

            off = sum(1 for h in unfiltered if h["pillar"] != expected_pillar)
            off_pillar_total += off
            print(f"    -> {off}/{TOP_K} unfiltered hits came from the wrong pillar")
        print()

    if len(queries) > 1:
        total = len(queries) * TOP_K
        print(
            f"Across {len(queries)} queries: {off_pillar_total}/{total} unfiltered hits "
            f"were off-pillar ({off_pillar_total / total:.0%})."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
