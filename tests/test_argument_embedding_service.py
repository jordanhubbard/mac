"""Unit tests for ArgumentEmbeddingService.

Mocks the embedding API and Qdrant to avoid external deps.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest.mock import MagicMock, patch
import sys

sys.path.insert(0, "/tmp/mac-deliberation/src")

from mac.argument_embedding_service import ArgumentEmbeddingService, EMBED_DIM


def _fake_embed(text: str) -> list[float]:
    """Deterministic fake 2048-dim embedding for testing."""
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    return [(seed * (i + 1) % 1000) / 1000.0 for i in range(EMBED_DIM)]


class TestArgumentEmbeddingService(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ArgumentEmbeddingService(worker_token="test_token")

    @patch.object(ArgumentEmbeddingService, "_embed")
    @patch.object(ArgumentEmbeddingService, "_upsert")
    def test_index_resolved_deliberation_embeds_and_upserts(
        self, mock_upsert: MagicMock, mock_embed: MagicMock
    ) -> None:
        mock_embed.side_effect = _fake_embed
        mock_upsert.return_value = None

        args = [
            {
                "id": "arg_a",
                "claim": "Empirical load test shows 50% latency reduction.",
                "argument_type": "empirical",
                "agent_id": "agent_natasha",
                "created_at": "2026-11-06T12:00:00Z",
            },
            {
                "id": "arg_b",
                "claim": "Architecture spec requires fan-out pattern.",
                "argument_type": "authority",
                "agent_id": "agent_rocky",
                "created_at": "2026-11-06T12:05:00Z",
            },
        ]

        n = self.svc.index_resolved_deliberation(
            deliberation_id="delib_test_001",
            resolution_path="authority",
            arguments=args,
        )

        self.assertEqual(n, 2)
        self.assertEqual(mock_embed.call_count, 2)
        self.assertEqual(mock_upsert.call_count, 1)

        # Verify the points passed to _upsert
        points = mock_upsert.call_args[0][0]
        self.assertEqual(len(points), 2)

        # Both points have correct payload structure
        for p in points:
            self.assertIn("deliberation_id", p["payload"])
            self.assertIn("resolution_path", p["payload"])
            self.assertIn("argument_claim", p["payload"])
            self.assertIn("argument_type", p["payload"])
            self.assertEqual(p["payload"]["deliberation_id"], "delib_test_001")
            self.assertEqual(p["payload"]["resolution_path"], "authority")
            self.assertEqual(len(p["vector"]), EMBED_DIM)

    @patch.object(ArgumentEmbeddingService, "_embed")
    @patch.object(ArgumentEmbeddingService, "_upsert")
    def test_index_skips_empty_claims(self, mock_upsert: MagicMock, mock_embed: MagicMock) -> None:
        mock_embed.side_effect = _fake_embed
        args = [
            {"id": "arg_x", "claim": "", "argument_type": "principle", "agent_id": "a"},
            {"id": "arg_y", "claim": "   ", "argument_type": "principle", "agent_id": "a"},
        ]
        n = self.svc.index_resolved_deliberation(
            deliberation_id="delib_002",
            resolution_path="unanimity",
            arguments=args,
        )
        self.assertEqual(n, 0)
        mock_embed.assert_not_called()
        mock_upsert.assert_not_called()

    @patch.object(ArgumentEmbeddingService, "_embed")
    @patch.object(ArgumentEmbeddingService, "_upsert")
    def test_index_empty_arguments(self, mock_upsert: MagicMock, mock_embed: MagicMock) -> None:
        n = self.svc.index_resolved_deliberation(
            deliberation_id="delib_003",
            resolution_path="expiry",
            arguments=[],
        )
        self.assertEqual(n, 0)
        mock_embed.assert_not_called()

    def test_stable_id_deterministic(self) -> None:
        id1 = ArgumentEmbeddingService._stable_id("delib_a", "arg_1")
        id2 = ArgumentEmbeddingService._stable_id("delib_a", "arg_1")
        id3 = ArgumentEmbeddingService._stable_id("delib_a", "arg_2")
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)
        self.assertGreater(id1, 0)
        self.assertLess(id1, 2**31)

    @patch.object(ArgumentEmbeddingService, "_embed")
    @patch.object(ArgumentEmbeddingService, "_qdrant_request")
    def test_search_no_filter(self, mock_qdrant: MagicMock, mock_embed: MagicMock) -> None:
        mock_embed.side_effect = _fake_embed
        mock_qdrant.return_value = {
            "result": [
                {
                    "id": 1,
                    "score": 0.9,
                    "payload": {"argument_claim": "test claim", "argument_type": "empirical"},
                },
            ]
        }
        results = self.svc.search_similar_arguments("performance improvement", top_k=5)
        self.assertEqual(len(results), 1)
        # No filter in body
        call_body = mock_qdrant.call_args[0][2]
        self.assertNotIn("filter", call_body)

    @patch.object(ArgumentEmbeddingService, "_embed")
    @patch.object(ArgumentEmbeddingService, "_qdrant_request")
    def test_search_with_type_filter(self, mock_qdrant: MagicMock, mock_embed: MagicMock) -> None:
        mock_embed.side_effect = _fake_embed
        mock_qdrant.return_value = {"result": []}
        self.svc.search_similar_arguments(
            "authority argument", top_k=3, argument_type_filter="authority"
        )
        call_body = mock_qdrant.call_args[0][2]
        self.assertIn("filter", call_body)
        conditions = call_body["filter"]["must"]
        self.assertTrue(any(c["key"] == "argument_type" for c in conditions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
