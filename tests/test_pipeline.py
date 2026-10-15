"""Tests for the rules that decide what a review is allowed to report.

Each one covers a decision made because of a measurement: the relevance
threshold, the verification pass, and deduplication across specialists.
"""

from __future__ import annotations

from backend.agents.supervisor import _deduplicate, _same_problem
from backend.agents.verify import verify
from backend.core.models import Citation, Edge, Finding, Graph, Node, Pillar


def graph() -> Graph:
    return Graph(
        nodes=[
            Node(id="web", service="EC2", label="EC2 m5.2xlarge, running 24/7, ~8% CPU"),
            Node(id="db", service="RDS", label="RDS PostgreSQL single-AZ, no automated backups"),
        ],
        edges=[Edge(source="web", target="db", label="SQL")],
    )


def finding(issue: str, evidence: list[str], pillar=Pillar.SECURITY, severity="medium") -> Finding:
    return Finding(pillar=pillar, issue=issue, evidence=evidence, severity=severity)


class TestGraphText:
    def test_labels_are_verbatim(self):
        """The reviewable detail lives in the labels; summarising hides it."""
        text = graph().as_text()
        assert "running 24/7, ~8% CPU" in text
        assert "single-AZ, no automated backups" in text

    def test_edges_are_listed(self):
        assert "web -> db (SQL)" in graph().as_text()


class TestVerification:
    def test_drops_finding_whose_evidence_does_not_exist(self, monkeypatch):
        """Checked in code, with no model call: the evidence is simply absent."""

        def fail(*args, **kwargs):
            raise AssertionError("should not reach the model")

        monkeypatch.setattr("backend.agents.verify.converse", fail)
        kept, rejected = verify(graph(), [finding("Something about a queue", ["sqs"])])
        assert kept == []
        assert len(rejected) == 1

    def test_keeps_supported_finding(self, monkeypatch):
        monkeypatch.setattr(
            "backend.agents.verify.converse",
            lambda *a, **k: '{"verdicts": [{"n": 1, "supported": true, "why": "label says single-AZ"}]}',
        )
        kept, rejected = verify(graph(), [finding("Database is in one AZ", ["db"])])
        assert len(kept) == 1
        assert rejected == []

    def test_drops_unsupported_finding(self, monkeypatch):
        monkeypatch.setattr(
            "backend.agents.verify.converse",
            lambda *a, **k: '{"verdicts": [{"n": 1, "supported": false, "why": "not stated"}]}',
        )
        kept, rejected = verify(graph(), [finding("Traffic is unencrypted", ["web"])])
        assert kept == []
        assert len(rejected) == 1

    def test_unjudged_finding_is_dropped(self, monkeypatch):
        """Silence is not approval — an unchecked claim is what this pass stops."""
        monkeypatch.setattr(
            "backend.agents.verify.converse", lambda *a, **k: '{"verdicts": []}'
        )
        kept, rejected = verify(graph(), [finding("Something", ["web"])])
        assert kept == []
        assert len(rejected) == 1

    def test_empty_input_makes_no_call(self, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("should not reach the model")

        monkeypatch.setattr("backend.agents.verify.converse", fail)
        assert verify(graph(), []) == ([], [])


class TestDeduplication:
    def test_same_service_same_topic_is_one_finding(self):
        a = finding("S3 bucket is publicly readable", ["assets"], Pillar.SECURITY)
        b = finding("Public read enabled on S3 bucket", ["assets"], Pillar.COST)
        assert _same_problem(a, b)
        assert len(_deduplicate([a, b])) == 1

    def test_same_service_different_topic_is_two_findings(self):
        """One service can genuinely have two unrelated problems."""
        a = finding("Instance is oversized for its utilisation", ["web"], Pillar.COST)
        b = finding("Instance sits in a single availability zone", ["web"], Pillar.RELIABILITY)
        assert not _same_problem(a, b)
        assert len(_deduplicate([a, b])) == 2

    def test_same_topic_different_service_is_two_findings(self):
        a = finding("Not encrypted at rest", ["db"])
        b = finding("Not encrypted at rest", ["assets"])
        assert not _same_problem(a, b)
        assert len(_deduplicate([a, b])) == 2

    def test_higher_severity_survives(self):
        low = finding("S3 bucket is publicly readable", ["assets"], severity="low")
        high = finding("S3 bucket is publicly readable", ["assets"], severity="high")
        assert _deduplicate([low, high])[0].severity == "high"

    def test_cited_finding_beats_uncited_at_equal_severity(self):
        uncited = finding("S3 bucket is publicly readable", ["assets"])
        cited = finding("S3 bucket is publicly readable", ["assets"])
        cited.citation = Citation(
            bp_id="SEC01-BP01", bp_title="Separate workloads", page=14, pillar=Pillar.SECURITY
        )
        assert _deduplicate([uncited, cited])[0].citation is not None

    def test_findings_without_evidence_are_kept(self):
        """They cannot be matched, and dropping them would hide rather than merge."""
        a = finding("Something general", [])
        b = finding("Something else general", [])
        assert len(_deduplicate([a, b])) == 2
