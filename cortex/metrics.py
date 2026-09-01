"""Prometheus metrics (FR-16): event append rate, librarian lag, search latency
histogram, queue depth, SSE subscribers."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

events_appended = Counter("cortex_events_appended_total", "Events appended", ["kind"])
events_appended_total = Counter("cortex_events_total", "Total events", ["kind"])

search_latency = Histogram(
    "cortex_search_latency_seconds",
    "Hybrid search latency",
    buckets=(0.01, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 2.0, 5.0),
)

append_latency = Histogram(
    "cortex_append_latency_seconds",
    "Event append latency",
    buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0),
)

librarian_lag = Gauge("cortex_librarian_lag_seconds", "Seconds since oldest unprocessed event")
librarian_processed = Counter("cortex_librarian_processed_total", "Events processed by librarian")
librarian_errors = Counter("cortex_librarian_errors_total", "Librarian processing errors")

queue_depth = Gauge("cortex_queue_depth", "Open queue items", ["kind"])
sse_subscribers = Gauge("cortex_sse_subscribers", "Active SSE subscribers")
agents_seen = Gauge("cortex_agents_seen", "Registered agents with last_seen set")
