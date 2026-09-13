"""Semantic map projection + payload builder (cortex/mapproj.py).

These run whether or not umap-learn/scikit-learn are installed — the point of
the fallbacks is that the map still renders, so the tests assert on properties
that hold for either path.
"""

import numpy as np
import pytest

from cortex.mapproj import (
    EXTENT,
    assign_clusters,
    build_map,
    method,
    name_clusters,
    project_vectors,
)


class Row(dict):
    """Fake asyncpg Record (dict-style access), as in test_graph.py."""
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


def blobs(n_per=12, dim=32, seed=0):
    """Three well-separated clusters in `dim` dimensions."""
    rng = np.random.default_rng(seed)
    centres = np.eye(3, dim) * 10
    return np.vstack([c + rng.normal(0, 0.35, (n_per, dim)) for c in centres])


# ── projection ────────────────────────────────────────────────────────────────

def test_projects_to_two_dimensions():
    coords = project_vectors(blobs())
    assert coords.shape == (36, 2)
    assert np.isfinite(coords).all()


def test_coordinates_stay_inside_the_world_box():
    coords = project_vectors(blobs())
    assert np.abs(coords).max() <= EXTENT + 1e-6


def test_projection_is_deterministic():
    X = blobs()
    a, b = project_vectors(X), project_vectors(X)
    np.testing.assert_allclose(a, b)


def test_separated_input_stays_separated():
    """Whichever backend runs, points from one blob must land nearer each
    other than the blobs land from one another — that is the whole claim the
    map makes to the reader."""
    coords = project_vectors(blobs())
    groups = [coords[0:12], coords[12:24], coords[24:36]]
    within = np.mean([np.linalg.norm(g - g.mean(axis=0), axis=1).mean() for g in groups])
    centres = np.array([g.mean(axis=0) for g in groups])
    between = np.mean([np.linalg.norm(centres[i] - centres[j])
                       for i in range(3) for j in range(i + 1, 3)])
    assert between > within * 2


@pytest.mark.parametrize("n", [0, 1, 2])
def test_degenerate_inputs_do_not_raise(n):
    coords = project_vectors(np.ones((n, 8)))
    assert coords.shape == (n, 2)


def test_rejects_non_matrix_input():
    with pytest.raises(ValueError):
        project_vectors(np.ones(8))


def test_method_names_both_halves():
    assert "+" in method()


def test_method_reports_the_backend_that_is_actually_installed():
    """Guards an argument-order slip that reported `pca` on a box where UMAP
    was installed and being used — a silently wrong label on every map."""
    import importlib.util

    projector, clusterer = method().split("+")
    assert projector == ("umap" if importlib.util.find_spec("umap") else "pca")
    assert clusterer == ("hdbscan" if importlib.util.find_spec("sklearn") else "kmeans")


def test_method_does_not_import_the_heavy_backends():
    """method() runs on every /map render; importing umap drags in numba and
    llvmlite, which cost ~45s on first import and made the page look hung."""
    import sys

    for mod in ("umap", "numba", "llvmlite"):
        sys.modules.pop(mod, None)
    from cortex import mapproj
    mapproj._available.cache_clear()
    mapproj.method()
    assert "umap" not in sys.modules


# ── clustering + naming ───────────────────────────────────────────────────────

def test_clusters_are_found_and_cover_the_points():
    coords = project_vectors(blobs())
    labels = assign_clusters(coords)
    assert len(labels) == len(coords)
    assert len({int(x) for x in labels} - {-1}) >= 2


def test_too_few_points_cluster_to_nothing():
    assert list(assign_clusters(np.zeros((2, 2)))) == [-1, -1]


def test_regions_are_named_after_their_best_connected_member():
    coords = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    items = [{"label": "minor", "weight": 1}, {"label": "HeyGen", "weight": 40},
             {"label": "other", "weight": 2}]
    (region,) = name_clusters([0, 0, 0], coords, items)
    assert region["name"] == "HeyGen"
    assert region["size"] == 3
    assert region["radius"] > 0


def test_entities_outrank_documents_as_region_anchors():
    coords = np.array([[0.0, 0.0], [1.0, 0.0]])
    items = [{"label": "a-doc.md", "weight": 99, "is_entity": False},
             {"label": "Docker", "weight": 2, "is_entity": True}]
    (region,) = name_clusters([0, 0], coords, items)
    assert region["name"] == "Docker"


def test_long_region_names_are_truncated():
    coords = np.array([[0.0, 0.0], [1.0, 0.0]])
    items = [{"label": "x" * 80, "weight": 1}] * 2
    (region,) = name_clusters([0, 0], coords, items)
    assert len(region["name"]) <= 34 and region["name"].endswith("…")


def test_unclustered_points_produce_no_region():
    assert name_clusters([-1, -1], np.zeros((2, 2)), [{"label": "a"}, {"label": "b"}]) == []


# ── payload builder ───────────────────────────────────────────────────────────

def ent(id, name, etype="tool", x=1.0, y=2.0, cluster=0, facts=3, doclinks=1):
    return Row({"id": id, "name": name, "etype": etype, "summary": "s",
                "project": "cortex", "map_x": x, "map_y": y,
                "map_cluster": cluster, "fact_count": facts, "doclink_count": doclinks})


def note(id, title, x=3.0, y=4.0, cluster=1, note_type="note"):
    return Row({"id": id, "title": title, "original_filename": None,
                "note_type": note_type, "project": "cortex", "snippet": "body",
                "map_x": x, "map_y": y, "map_cluster": cluster, "doclink_count": 2})


def test_build_map_shapes_points_and_counts():
    doc = build_map([ent("e1", "Docker")], [note("n1", "Notes")])
    assert doc["stats"]["entities"] == 1 and doc["stats"]["docs"] == 1
    ids = {p["id"] for p in doc["points"]}
    assert ids == {"e1", "doc:n1"}  # docs are prefixed, as in graph.build_graph


def test_build_map_skips_points_without_coordinates():
    doc = build_map([ent("e1", "Docker", x=None, y=None)], [])
    assert doc["points"] == [] and doc["stats"]["entities"] == 0


def test_build_map_keeps_only_links_whose_endpoints_are_present():
    links = [Row({"note_id": "n1", "entity_id": "e1", "score": 0.9, "method": "embedding"}),
             Row({"note_id": "n1", "entity_id": "missing", "score": 0.8, "method": "embedding"})]
    doc = build_map([ent("e1", "Docker")], [note("n1", "Notes")], links)
    assert len(doc["links"]) == 1
    assert doc["links"][0] == {"source": "doc:n1", "target": "e1",
                              "score": 0.9, "method": "embedding"}


def test_untyped_entities_get_the_muted_swatch():
    doc = build_map([ent("e1", "mystery", etype=None)], [])
    assert doc["points"][0]["etype"] == "untyped"
    assert {t["name"]: t["color"] for t in doc["types"]}["untyped"] == "#565f73"


def test_type_counts_are_reported():
    doc = build_map([ent("e1", "Docker", etype="tech"), ent("e2", "Redis", etype="tech")], [])
    assert {t["name"]: t["count"] for t in doc["types"]} == {"tech": 2}
