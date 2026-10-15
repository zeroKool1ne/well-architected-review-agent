"""Runtime configuration, read once from the environment.

Everything that differs between a laptop and Lambda lives here, so no module
below has to know which it is running on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    region: str

    # Bedrock. The "us." prefix selects an inference profile; current models
    # reject their bare ids with a ValidationException.
    chat_model: str
    embed_model: str

    # Where the exported index lives. On Lambda it is pulled from S3 into /tmp
    # at cold start, because the function's own filesystem is read-only.
    index_dir: Path
    index_bucket: str | None
    index_key: str

    # Uploads and human-in-the-loop state.
    upload_bucket: str | None
    state_table: str | None

    # A pillar whose best match is further away than this has nothing to say
    # about the topic. Measured on 07.10: forced perspectives came back around
    # 0.87 cosine distance, and without a floor a specialist invents a finding
    # rather than reporting none.
    relevance_threshold: float

    @property
    def is_lambda(self) -> bool:
        return bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def load() -> Settings:
    on_lambda = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

    return Settings(
        region=os.getenv("AWS_REGION", "us-east-1"),
        chat_model=os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0"),
        embed_model=os.getenv("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0"),
        index_dir=Path("/tmp/index") if on_lambda else ROOT / "data" / "index",
        index_bucket=os.getenv("INDEX_BUCKET"),
        index_key=os.getenv("INDEX_KEY", "index/index.tar.gz"),
        upload_bucket=os.getenv("UPLOAD_BUCKET"),
        state_table=os.getenv("STATE_TABLE"),
        relevance_threshold=float(os.getenv("RELEVANCE_THRESHOLD", "0.75")),
    )


settings = load()
