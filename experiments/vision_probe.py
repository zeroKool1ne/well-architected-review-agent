"""Spike: can Bedrock Haiku read an AWS architecture diagram well enough to review it?"""
import json
import sys
import time

import boto3

MODEL_ID = "us.amazon.nova-lite-v1:0"
PROMPT = """You are reading an AWS architecture diagram.

Extract what you actually see. Return ONLY valid JSON:
{
  "services": [{"name": "<AWS service>", "label": "<text on the box>", "confidence": 0.0-1.0}],
  "connections": [{"from": "<box>", "to": "<box>", "label": "<arrow label or null>"}],
  "unreadable_regions": ["<describe anything you cannot read with confidence>"]
}

Be honest about confidence. If a box is ambiguous, say so in unreadable_regions
rather than guessing."""


def main(path: str) -> None:
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    with open(path, "rb") as fh:
        image_bytes = fh.read()

    started = time.perf_counter()
    response = client.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": image_bytes}}},
                    {"text": PROMPT},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 2000, "temperature": 0},
    )
    elapsed = time.perf_counter() - started

    usage = response["usage"]
    print(f"--- latency: {elapsed:.2f}s | tokens in/out: {usage['inputTokens']}/{usage['outputTokens']} ---")
    print(response["output"]["message"]["content"][0]["text"])


if __name__ == "__main__":
    main(sys.argv[1])
