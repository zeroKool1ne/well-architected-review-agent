"""Tests for the JSON recovery and the filters that drop bad model output.

These cover the failure modes actually observed while building the pipeline,
not hypothetical ones: models wrapping JSON in prose or fences, dropping the
requested envelope, and naming services that do not exist in the diagram.
"""

from __future__ import annotations

import pytest

from backend.agents.extraction import _items, _parse_json, extract_edges
from backend.core.models import Node


class TestParseJson:
    def test_plain_object(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert _parse_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_wrapped_in_prose(self):
        raw = 'Here is the result:\n\n{"a": 1}\n\nLet me know if you need more.'
        assert _parse_json(raw) == {"a": 1}

    def test_bare_array(self):
        """The envelope is requested but not always delivered."""
        assert _parse_json('[{"n": 1}]') == [{"n": 1}]

    def test_array_wrapped_in_prose(self):
        assert _parse_json('Sure:\n[{"n": 1}, {"n": 2}]') == [{"n": 1}, {"n": 2}]

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON"):
            _parse_json("I could not read the diagram.")


class TestItems:
    def test_reads_nested_list(self):
        assert _items({"findings": [1, 2]}, "findings") == [1, 2]

    def test_accepts_bare_list(self):
        """A model that drops the envelope should not cost us the data."""
        assert _items([1, 2], "findings") == [1, 2]

    def test_missing_key_is_empty(self):
        assert _items({"other": [1]}, "findings") == []

    def test_non_list_value_is_empty(self):
        assert _items({"findings": "none"}, "findings") == []


class TestEdgeFiltering:
    """extract_edges drops edges that cannot be true of the given nodes.

    An edge naming a box that was never extracted is a hallucination with a
    cheap tell. Catching it in code means it never reaches the reviewer, who
    has a limited budget of attention for corrections.
    """

    NODES = [
        Node(id="alb", service="ELB", label="Load balancer"),
        Node(id="web", service="EC2", label="Web server"),
    ]

    def _edges(self, monkeypatch, payload: str):
        monkeypatch.setattr("backend.agents.extraction.converse", lambda *a, **k: payload)
        return extract_edges(b"fake-image", self.NODES)

    def test_keeps_valid_edge(self, monkeypatch):
        edges = self._edges(monkeypatch, '{"edges": [{"source": "alb", "target": "web"}]}')
        assert len(edges) == 1
        assert (edges[0].source, edges[0].target) == ("alb", "web")

    def test_drops_edge_to_unknown_node(self, monkeypatch):
        edges = self._edges(monkeypatch, '{"edges": [{"source": "alb", "target": "ghost"}]}')
        assert edges == []

    def test_drops_self_loop(self, monkeypatch):
        edges = self._edges(monkeypatch, '{"edges": [{"source": "alb", "target": "alb"}]}')
        assert edges == []

    def test_deduplicates(self, monkeypatch):
        payload = '{"edges": [{"source": "alb", "target": "web"}, {"source": "alb", "target": "web"}]}'
        assert len(self._edges(monkeypatch, payload)) == 1

    def test_empty_is_accepted(self, monkeypatch):
        """Reporting no arrows is an allowed answer, not a failure."""
        assert self._edges(monkeypatch, '{"edges": []}') == []

    def test_single_node_skips_the_call(self, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("should not call the model for one node")

        monkeypatch.setattr("backend.agents.extraction.converse", fail)
        assert extract_edges(b"fake-image", self.NODES[:1]) == []
