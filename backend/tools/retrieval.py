"""search_pillars — the tool the specialist agents use to find rules.

Filtering by pillar is what makes a specialist a specialist. Measured on
07.10.2026: for unambiguous questions the filter changes nothing, but for
topics that belong to several pillars an unfiltered search returns one pillar
only. Multi-AZ retrieves reliability guidance and never surfaces the cost of
cross-zone traffic; database backups retrieve reliability and never ask whether
the backups are encrypted. Filtering does not improve hits, it forces
perspectives that would otherwise stay invisible.

The same measurement set the floor for staying silent: where a pillar has
nothing to say, its closest passage sits around 0.87 cosine distance. Handing
those to the model would invite it to construct a finding from unrelated text,
so they are withheld instead.

Two backends behind one function. Chroma is the vector database the corpus is
built into and the one used during development. The deployed function reads the
same index exported as a normalised array, because chromadb carries 82 MB of
ONNX runtime for an embedding model this project never calls — every embedding
comes from Bedrock. At 1,297 vectors the search is a dot product either way.
"""

from __future__ import annotations

import functools
import json
import tarfile
import tempfile
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from backend.core.aws import embed, s3
from backend.core.config import ROOT, settings
from backend.core.models import Citation, Pillar

COLLECTION = "well_architected"
ARRAY_DIR = ROOT / "data" / "index"


class Passage(BaseModel):
    text: str
    distance: float
    citation: Citation

    def cite(self) -> str:
        return f'{self.citation.bp_id} "{self.citation.bp_title}" (p. {self.citation.page})'


def _fetch_index(target: Path) -> Path:
    """Pull the index from S3 when it is not on disk.

    Lambda's filesystem is read-only apart from /tmp, and /tmp survives between
    invocations on a warm container, so this runs once per cold start.
    """
    if target.exists() and any(target.iterdir()):
        return target

    if not settings.index_bucket:
        raise RuntimeError(
            f"No index at {target} and INDEX_BUCKET is unset. "
            "Run backend/ingestion/build_index.py, then export_index.py."
        )

    target.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
        s3().download_fileobj(settings.index_bucket, settings.index_key, archive)
        archive.flush()
        with tarfile.open(archive.name) as tar:
            tar.extractall(target.parent, filter="data")

    return target


@functools.cache
def _array_index() -> tuple[np.ndarray, list[dict]]:
    directory = _fetch_index(settings.index_dir)
    vectors = np.load(directory / "vectors.npy")
    chunks = json.loads((directory / "chunks.json").read_text())
    return vectors, chunks


@functools.cache
def _chroma():
    import chromadb

    return chromadb.PersistentClient(path=str(ROOT / "data" / "chroma")).get_collection(COLLECTION)


def _use_chroma() -> bool:
    """Prefer Chroma locally, fall back to the exported array."""
    if settings.is_lambda:
        return False
    return (ROOT / "data" / "chroma").exists() and not ARRAY_DIR.exists()


def _passage(text: str, distance: float, meta: dict) -> Passage:
    return Passage(
        text=text,
        distance=distance,
        citation=Citation(
            bp_id=meta["bp_id"],
            bp_title=meta["bp_title"],
            page=int(meta["page"]),
            pillar=Pillar(meta["pillar"]),
        ),
    )


def search_pillars(query: str, pillar: Pillar, limit: int = 4) -> list[Passage]:
    """Return guidance from one pillar, or nothing if the pillar is off topic."""
    vector = embed(query)

    if _use_chroma():
        result = _chroma().query(
            query_embeddings=[vector], n_results=limit, where={"pillar": str(pillar)}
        )
        candidates = list(
            zip(
                result["documents"][0],
                result["distances"][0],
                result["metadatas"][0],
                strict=False,
            )
        )
    else:
        vectors, chunks = _array_index()

        # Restrict to the pillar first: scoring the whole corpus and filtering
        # afterwards could return fewer than `limit` in-pillar hits.
        mask = np.array([c["pillar"] == str(pillar) for c in chunks])
        rows = np.flatnonzero(mask)

        query_vector = np.asarray(vector, dtype=np.float32)
        query_vector /= max(float(np.linalg.norm(query_vector)), 1e-12)

        # Vectors were normalised at export, so the dot product is the cosine
        # similarity, and 1 - similarity matches Chroma's cosine distance.
        similarity = vectors[rows] @ query_vector
        top = rows[np.argsort(-similarity)[:limit]]

        candidates = [
            (chunks[i]["text"], float(1.0 - (vectors[i] @ query_vector)), chunks[i])
            for i in top
        ]

    return [
        _passage(text, distance, meta)
        for text, distance, meta in candidates
        if distance <= settings.relevance_threshold
    ]
