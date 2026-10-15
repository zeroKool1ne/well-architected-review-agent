"""Where a review lives between the two requests that make it up.

The human gate is a boundary between calls, not a pause inside one: extraction
writes a graph and stops, a person corrects it, and only then does the review
run. That needs the graph to survive between requests, which rules out holding
it in memory — Lambda may answer the second call from a different container
than the first.

DynamoDB holds it. On a laptop without AWS the same interface writes JSON files
under data/state, so the whole flow can be exercised offline.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from backend.core.config import ROOT, settings
from backend.core.models import Graph

PENDING = "pending"
CONFIRMED = "confirmed"
REVIEWED = "reviewed"

LOCAL_DIR = ROOT / "data" / "state"

# Records expire a day after they are written. Nothing here is worth keeping,
# and an unbounded table of abandoned uploads is a cost and privacy problem.
TTL_SECONDS = 24 * 60 * 60


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _table():
    from backend.core.aws import dynamodb

    return dynamodb().Table(settings.state_table)


def put(record: dict[str, Any]) -> None:
    record.setdefault("created_at", int(time.time()))
    record["expires_at"] = int(time.time()) + TTL_SECONDS

    if settings.state_table:
        _table().put_item(Item=record)
        return

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / f"{record['review_id']}.json").write_text(json.dumps(record, indent=2))


def get(review_id: str) -> dict[str, Any] | None:
    if settings.state_table:
        return _table().get_item(Key={"review_id": review_id}).get("Item")

    path = LOCAL_DIR / f"{review_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def create_pending(review_id: str, graph: Graph, source: str) -> dict[str, Any]:
    record = {
        "review_id": review_id,
        "status": PENDING,
        "source": source,
        "graph": graph.model_dump(),
    }
    put(record)
    return record


def confirm(review_id: str, graph: Graph) -> dict[str, Any] | None:
    """Store the human-corrected graph and open the gate.

    The corrected graph replaces the extracted one rather than being merged
    into it: the person looking at the diagram is the authority on what it
    shows, and a merge would let the model's version survive in places nobody
    looked at.
    """
    record = get(review_id)
    if record is None:
        return None

    record["graph"] = graph.model_dump()
    record["status"] = CONFIRMED
    record["confirmed_at"] = int(time.time())
    put(record)
    return record


def save_findings(review_id: str, findings: list[dict], rejected: list[dict]) -> None:
    record = get(review_id)
    if record is None:
        return

    record["status"] = REVIEWED
    record["findings"] = findings
    record["rejected"] = rejected
    put(record)
