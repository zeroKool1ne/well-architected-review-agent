"""search_pillars — the tool the specialist agents use to find rules.

Filtering by pillar is what makes a specialist a specialist. Measured on
07.10.2026: for unambiguous questions the filter changes nothing, but for
topics that belong to several pillars an unfiltered search returns one pillar
only. Multi-AZ retrieves reliability guidance and never surfaces the cost of
cross-zone traffic; database backups retrieve reliability and never ask whether
the backups are encrypted. The filter does not improve hits, it forces
perspectives that would otherwise stay invisible.

The same measurement produced the relevance threshold: where a pillar has
nothing to say about a topic, its best match sits around 0.87 cosine distance.
Returning those anyway invites the agent to construct a finding out of
irrelevant text, so they are withheld instead.
"""

from __future__ import annotations

import functools
import tarfile
import tempfile
from pathlib import Path

import chromadb
from pydantic import BaseModel

from backend.core.aws import embed, s3
from backend.core.config import settings
from backend.core.models import Citation, Pillar

COLLECTION = "well_architected"


class Passage(BaseModel):
    text: str
    distance: float
    citation: Citation

    def cite(self) -> str:
        return f"{self.citation.bp_id} \"{self.citation.bp_title}\" (p. {self.citation.page})"


def _ensure_index() -> Path:
    """Make the index available locally, downloading it on Lambda if needed.

    The function filesystem is read-only apart from /tmp, and /tmp survives
    between invocations on a warm container, so this runs once per cold start.
    """
    target = settings.index_dir
    if target.exists() and any(target.iterdir()):
        return target

    if not settings.index_bucket:
        raise RuntimeError(
            f"No index at {target} and INDEX_BUCKET is unset. "
            "Run backend/ingestion/build_index.py first."
        )

    target.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
        s3().download_fileobj(settings.index_bucket, settings.index_key, archive)
        archive.flush()
        with tarfile.open(archive.name) as tar:
            tar.extractall(target.parent, filter="data")

    return target


@functools.cache
def _collection():
    return chromadb.PersistentClient(path=str(_ensure_index())).get_collection(COLLECTION)


def search_pillars(query: str, pillar: Pillar, limit: int = 4) -> list[Passage]:
    """Return guidance from one pillar, or nothing if the pillar is off topic."""
    result = _collection().query(
        query_embeddings=[embed(query)],
        n_results=limit,
        where={"pillar": str(pillar)},
    )

    passages: list[Passage] = []
    for text, distance, meta in zip(
        result["documents"][0], result["distances"][0], result["metadatas"][0]
    ):
        if distance > settings.relevance_threshold:
            continue
        passages.append(
            Passage(
                text=text,
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
