"""Tests for the AWS-facing parts, with AWS mocked.

moto stands in for S3 and DynamoDB so these run offline, in CI, and without
spending anything. What they check is the application's own logic around those
services — that state survives the gate, that the gate cannot be skipped, and
that the API rejects what it should before any model is called.
"""

from __future__ import annotations

import os

import pytest

# moto's mocks must be in place before boto3 clients are created, and the
# application creates its clients lazily but caches them.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402

from backend.core.models import Edge, Graph, Node  # noqa: E402

TABLE = "test-reviews"


def sample_graph() -> Graph:
    return Graph(
        nodes=[
            Node(id="web", service="EC2", label="EC2, running 24/7"),
            Node(id="db", service="RDS", label="RDS single-AZ"),
        ],
        edges=[Edge(source="web", target="db", label="SQL")],
    )


@pytest.fixture
def dynamo(monkeypatch):
    """A mocked table, with the store pointed at it."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "review_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "review_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )

        from dataclasses import replace

        from backend.core import aws, store

        aws.dynamodb.cache_clear()
        # Settings is frozen on purpose, so swap the whole object rather than
        # poking a field into it.
        monkeypatch.setattr(store, "settings", replace(store.settings, state_table=TABLE))
        yield store
        aws.dynamodb.cache_clear()


class TestStateStore:
    def test_pending_round_trips(self, dynamo):
        dynamo.create_pending("abc123", sample_graph(), source="diagram.png")
        record = dynamo.get("abc123")

        assert record["status"] == dynamo.PENDING
        assert record["source"] == "diagram.png"
        assert len(record["graph"]["nodes"]) == 2

    def test_unknown_id_returns_none(self, dynamo):
        assert dynamo.get("does-not-exist") is None

    def test_confirm_replaces_the_graph(self, dynamo):
        """The person reading the diagram is the authority on what it shows.

        A merge would let the model's version survive in places nobody looked.
        """
        dynamo.create_pending("abc123", sample_graph(), source="x.png")

        corrected = Graph(nodes=[Node(id="web", service="EC2", label="EC2")], edges=[])
        dynamo.confirm("abc123", corrected)

        record = dynamo.get("abc123")
        assert record["status"] == dynamo.CONFIRMED
        assert len(record["graph"]["nodes"]) == 1
        assert record["graph"]["edges"] == []

    def test_confirm_on_unknown_id_is_none(self, dynamo):
        assert dynamo.confirm("nope", sample_graph()) is None

    def test_records_carry_a_ttl(self, dynamo):
        """An unbounded table of abandoned uploads is a cost and privacy problem."""
        dynamo.create_pending("abc123", sample_graph(), source="x.png")
        record = dynamo.get("abc123")
        assert record["expires_at"] > record["created_at"]


class TestApiGuards:
    """What the endpoints reject before any model is called."""

    @pytest.fixture
    def client(self):
        from backend.api.main import app

        return TestClient(app)

    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_rejects_unsupported_image_type(self, client):
        response = client.post(
            "/extract", files={"file": ("diagram.pdf", b"not an image", "application/pdf")}
        )
        assert response.status_code == 415
        assert "pdf" in response.json()["detail"]

    def test_rejects_empty_file(self, client):
        response = client.post("/extract", files={"file": ("d.png", b"", "image/png")})
        assert response.status_code == 400

    def test_rejects_oversized_image(self, client):
        response = client.post(
            "/extract", files={"file": ("d.png", b"x" * (6 * 1024 * 1024), "image/png")}
        )
        assert response.status_code == 413

    def test_review_of_unknown_id_is_404(self, client):
        response = client.post(
            "/review/does-not-exist", json={"graph": sample_graph().model_dump()}
        )
        assert response.status_code == 404

    def test_review_rejects_empty_graph(self, client, dynamo):
        """A confirmed graph with no services is a correction gone wrong."""
        dynamo.create_pending("abc123", sample_graph(), source="x.png")
        response = client.post(
            "/review/abc123", json={"graph": {"nodes": [], "edges": [], "unreadable": []}}
        )
        assert response.status_code == 400
