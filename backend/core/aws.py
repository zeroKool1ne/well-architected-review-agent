"""AWS clients and the two Bedrock calls everything else is built on.

Clients are created once per process. On Lambda that means once per cold start,
which is where most of the setup cost sits.
"""

from __future__ import annotations

import functools
import json

import boto3
from botocore.config import Config

from backend.core.config import settings

# Bedrock occasionally throttles under burst; adaptive retries handle it without
# a hand-rolled backoff. The read timeout is generous because a vision call on a
# large diagram can take several seconds.
_BEDROCK_CONFIG = Config(
    retries={"max_attempts": 6, "mode": "adaptive"},
    read_timeout=120,
    connect_timeout=10,
)


@functools.cache
def bedrock():
    return boto3.client("bedrock-runtime", region_name=settings.region, config=_BEDROCK_CONFIG)


@functools.cache
def s3():
    return boto3.client("s3", region_name=settings.region)


@functools.cache
def dynamodb():
    return boto3.resource("dynamodb", region_name=settings.region)


def embed(text: str) -> list[float]:
    """Turn text into the vector used to search the index."""
    response = bedrock().invoke_model(
        modelId=settings.embed_model,
        body=json.dumps({"inputText": text}),
    )
    return json.loads(response["body"].read())["embedding"]


def converse(
    prompt: str,
    *,
    system: str | None = None,
    image: bytes | None = None,
    image_format: str = "png",
    max_tokens: int = 2000,
    temperature: float = 0.0,
) -> str:
    """One turn against the chat model, optionally with an image attached.

    Temperature is 0 by default: an architecture review should return the same
    answer for the same diagram, and variation here would be a defect rather
    than a feature.
    """
    content: list[dict] = []
    if image is not None:
        content.append({"image": {"format": image_format, "source": {"bytes": image}}})
    content.append({"text": prompt})

    kwargs: dict = {
        "modelId": settings.chat_model,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if system:
        kwargs["system"] = [{"text": system}]

    response = bedrock().converse(**kwargs)
    return response["output"]["message"]["content"][0]["text"]
