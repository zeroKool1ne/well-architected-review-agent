"""AWS clients, the two Bedrock calls everything is built on, and their cost.

Clients are created once per process. On Lambda that means once per cold start,
which is where most of the setup cost sits.

Every call records its tokens. Cost per review is one of the things this
project is assessed on, and an estimate from a price list is weaker than a
count of what was actually spent.
"""

from __future__ import annotations

import functools
import json
import os
import threading
from dataclasses import dataclass, field

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

# USD per million tokens, us-east-1, October 2026.
PRICING = {
    "nova-lite": {"input": 0.06, "output": 0.24},
    "titan-embed-text-v2": {"input": 0.02, "output": 0.0},
}


@dataclass
class Usage:
    """Tokens and calls accumulated since the last reset."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    embed_calls: int = 0
    embed_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_chat(self, input_tokens: int, output_tokens: int) -> None:
        # The pillar specialists run concurrently, so this is contended.
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def record_embed(self, tokens: int) -> None:
        with self._lock:
            self.embed_calls += 1
            self.embed_tokens += tokens

    def reset(self) -> None:
        with self._lock:
            self.calls = self.input_tokens = self.output_tokens = 0
            self.embed_calls = self.embed_tokens = 0

    @property
    def usd(self) -> float:
        chat = PRICING["nova-lite"]
        embed = PRICING["titan-embed-text-v2"]
        return (
            self.input_tokens * chat["input"]
            + self.output_tokens * chat["output"]
            + self.embed_tokens * embed["input"]
        ) / 1_000_000

    def summary(self) -> dict:
        return {
            "chat_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embed_calls": self.embed_calls,
            "embed_tokens": self.embed_tokens,
            "usd": round(self.usd, 6),
        }


usage = Usage()


@functools.cache
def bedrock():
    return boto3.client("bedrock-runtime", region_name=settings.region, config=_BEDROCK_CONFIG)


@functools.cache
def s3():
    return boto3.client("s3", region_name=settings.region)


@functools.cache
def dynamodb():
    return boto3.resource("dynamodb", region_name=settings.region)


def _traceable(name: str):
    """Wrap a function for LangSmith when it is configured, otherwise leave it.

    Tracing is useful and optional. Making it a hard dependency would mean the
    pipeline cannot run without an account elsewhere, which is the wrong trade
    for a step that only observes.
    """

    def decorate(func):
        if not os.getenv("LANGSMITH_API_KEY"):
            return func
        try:
            from langsmith import traceable

            return traceable(name=name, run_type="llm")(func)
        except ImportError:
            return func

    return decorate


@_traceable("bedrock.embed")
def embed(text: str) -> list[float]:
    """Turn text into the vector used to search the index."""
    response = bedrock().invoke_model(
        modelId=settings.embed_model,
        body=json.dumps({"inputText": text}),
    )
    payload = json.loads(response["body"].read())
    usage.record_embed(int(payload.get("inputTextTokenCount", 0)))
    return payload["embedding"]


@_traceable("bedrock.converse")
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

    tokens = response.get("usage", {})
    usage.record_chat(int(tokens.get("inputTokens", 0)), int(tokens.get("outputTokens", 0)))

    return response["output"]["message"]["content"][0]["text"]
