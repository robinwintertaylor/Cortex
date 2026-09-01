"""Cortex — self-hosted shared second brain for AI agent harnesses.

Append-only event log (events are truth) + bi-temporal knowledge graph
(entities/facts with validity windows + provenance) + hybrid retrieval
(BM25 + vector + graph, RRF fusion). Three doors: MCP /mcp, REST /v1/*, SSE
/v1/stream. See plans/prd-cortex.md for the normative spec.
"""

__version__ = "1.0.0"
