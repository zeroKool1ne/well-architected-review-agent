"""Export the Chroma index to a flat array for the deployed function.

Chroma is the vector database: it holds the corpus, answers similarity queries
during development, and is what the index is built into. It is not, however,
what the function should carry. chromadb pulls in onnxruntime for its built-in
embedding function — 82 MB of a model this project never calls, because every
embedding comes from Bedrock. Shipping it would cost package size and cold
start for nothing.

At 1,297 vectors the search itself does not need a library either. A dot
product against a normalised matrix is a few milliseconds of numpy, and it
removes a dependency from the critical path.

So the index is built with Chroma and deployed as an array:

    python backend/ingestion/export_index.py
"""

from __future__ import annotations

import json
from pathlib import Path

import chromadb
import numpy as np

from backend.core.config import ROOT

COLLECTION = "well_architected"
SOURCE = ROOT / "data" / "chroma"
TARGET = ROOT / "data" / "index"


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"No Chroma index at {SOURCE}. Run build_index.py first.")

    collection = chromadb.PersistentClient(path=str(SOURCE)).get_collection(COLLECTION)
    data = collection.get(include=["documents", "metadatas", "embeddings"])

    vectors = np.asarray(data["embeddings"], dtype=np.float32)

    # Pre-normalise so similarity is a plain dot product at query time rather
    # than a division per row on every search.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.clip(norms, 1e-12, None)

    TARGET.mkdir(parents=True, exist_ok=True)
    np.save(TARGET / "vectors.npy", vectors)

    (TARGET / "chunks.json").write_text(
        json.dumps(
            [
                {"text": text, **meta}
                for text, meta in zip(data["documents"], data["metadatas"])
            ]
        )
    )

    size = sum(f.stat().st_size for f in TARGET.iterdir())
    print(
        f"  {len(vectors)} vectors x {vectors.shape[1]} dims -> {TARGET} "
        f"({size / 1e6:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
