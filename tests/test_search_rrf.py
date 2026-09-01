"""Unit tests for RRF fusion + recency prior (FR-6)."""

from datetime import datetime, timedelta, timezone

from cortex.search import _recency_prior, rrf_fuse


def _lists():
    # three legs of ranked results with overlap
    semantic = [("a", {"t": "a"}), ("b", {"t": "b"}), ("c", {"t": "c"})]
    keyword = [("b", {"t": "b"}), ("d", {"t": "d"})]
    graph = [("a", {"t": "a"}), ("e", {"t": "e"})]
    return [semantic, keyword, graph]


def test_rrf_overlap_ranks_first():
    fused = rrf_fuse(_lists())
    keys = [k for k, _m, _s in fused]
    # 'a' and 'b' appear in two lists each → top of the fusion
    assert set(keys[:2]) == {"a", "b"}
    assert keys[-1] in ("c", "d", "e")


def test_rrf_scores_are_summed_reciprocals():
    fused = rrf_fuse([[("x", {"t": "x"})], [("x", {"t": "x"})]])
    (_k, _m, score) = fused[0]
    assert abs(score - 2 * (1 / (60 + 1))) < 1e-9


def test_rrf_keeps_first_payload_for_duplicate_keys():
    fused = rrf_fuse([[("x", {"t": "first"})], [("x", {"t": "second"})]])
    assert fused[0][1]["t"] == "first"


def test_recency_prior_favors_fresh():
    now = datetime.now(timezone.utc)
    fresh = _recency_prior(now)
    old = _recency_prior(now - timedelta(days=365))
    assert fresh > old
    assert _recency_prior(None) == 1.0
    assert 1.0 <= fresh <= 2.0


def test_recency_prior_parses_iso_strings():
    now_iso = datetime.now(timezone.utc).isoformat()
    assert _recency_prior(now_iso) >= 1.0
    assert _recency_prior("garbage") == 1.0
