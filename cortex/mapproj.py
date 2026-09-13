"""Semantic map: project embeddings to stable 2D coordinates (FR-13 dashboard).

The knowledge graph's force-directed view could never be *learned* — every
reload re-ran the physics and moved everything. Here position carries meaning
instead: two points sit near each other because their embeddings are close, so
the layout is reproducible and the map is worth remembering.

Coordinates are cached on `entities.map_x/map_y` and `notes.map_x/map_y` and
recomputed by the librarian, so serving a map is a plain SELECT. They are a
projection of the embeddings like everything else outside `events` — a
`cortex rebuild --from 0` reproduces them.

UMAP gives markedly better separation and is used when importable (it ships in
the image via the `map` extra); PCA is the numpy-only fallback so the feature
degrades rather than disappearing. Same story for the clustering that gives the
map its labelled regions: HDBSCAN when scikit-learn is present, otherwise a
small deterministic k-means. `method()` reports which pair actually ran.

Everything here is a pure function over arrays — no DB, no I/O — so it unit
tests without a live stack (see tests/test_mapproj.py).
"""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from typing import Any, Iterable, Sequence

import numpy as np

# world-space half-extent; the UI fits this box to the viewport
EXTENT = 1000.0
SEED = 42


def method() -> str:
    """Which projection/clustering pair this deployment will actually use.

    Checks for the modules with find_spec rather than importing them: `method`
    is called on the read path (every /map render) and importing umap drags in
    numba + llvmlite, whose first import costs tens of seconds. The real import
    happens only in project_vectors, which runs in the librarian or CLI where
    paying that once is fine.
    """
    projector = "umap" if _available("umap") else "pca"
    clusterer = "hdbscan" if _available("sklearn") else "kmeans"
    return f"{projector}+{clusterer}"


@lru_cache(maxsize=None)
def _available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def project_vectors(vectors: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    """Embeddings → (n, 2) coordinates scaled into the [-EXTENT, EXTENT] box.

    Degenerate inputs (0, 1 or 2 points) can't be projected meaningfully and
    are placed deterministically rather than raising — the map should render
    on a nearly-empty brain too.
    """
    X = np.asarray(vectors, dtype=np.float64)
    if X.ndim != 2:
        raise ValueError(f"expected a 2-D array of vectors, got shape {X.shape}")
    n = X.shape[0]
    if n == 0:
        return np.zeros((0, 2))
    if n == 1:
        return np.zeros((1, 2))
    if n == 2:
        return _rescale(np.array([[-1.0, 0.0], [1.0, 0.0]]))

    # cosine distance is the right metric for text embeddings; normalising up
    # front lets the PCA path inherit it too (on unit vectors, euclidean and
    # cosine geometry agree).
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)

    try:
        from umap import UMAP
        coords = UMAP(
            n_neighbors=min(12, max(2, n - 1)),
            min_dist=0.12,
            metric="cosine",
            n_components=2,
            random_state=SEED,
        ).fit_transform(X)
    except Exception:
        coords = _pca2(X)
    return _rescale(np.asarray(coords, dtype=np.float64))


def _pca2(X: np.ndarray) -> np.ndarray:
    """First two principal components, numpy only."""
    centered = X - X.mean(axis=0)
    # full_matrices=False keeps this cheap on 1024-d input
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return centered @ vt[:2].T


def _rescale(coords: np.ndarray) -> np.ndarray:
    """Centre on the origin and scale the widest axis to fill the world box."""
    out = coords - coords.mean(axis=0)
    span = np.abs(out).max()
    if span > 0:
        out = out / span * EXTENT
    return out


def assign_clusters(coords: np.ndarray, *, min_size: int = 3) -> np.ndarray:
    """Group points into visible regions. -1 marks an unclustered point.

    Clustering runs on the *projected* coordinates, not the source embeddings,
    so the labelled regions correspond to the blobs a reader can actually see.
    """
    pts = np.asarray(coords, dtype=np.float64)
    if len(pts) < min_size:
        return np.full(len(pts), -1, dtype=int)
    try:
        from sklearn.cluster import HDBSCAN
        return HDBSCAN(min_cluster_size=min_size, min_samples=1).fit_predict(pts)
    except Exception:
        return _kmeans(pts, k=max(1, min(30, len(pts) // min_size)))


def _kmeans(pts: np.ndarray, *, k: int, iters: int = 40) -> np.ndarray:
    """Deterministic k-means fallback (seeded init, fixed iteration count)."""
    if k <= 1:
        return np.zeros(len(pts), dtype=int)
    rng = np.random.default_rng(SEED)
    centres = pts[rng.choice(len(pts), size=k, replace=False)]
    labels = np.zeros(len(pts), dtype=int)
    for _ in range(iters):
        d = ((pts[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        new = d.argmin(axis=1)
        if np.array_equal(new, labels):
            break
        labels = new
        for i in range(k):
            members = pts[labels == i]
            if len(members):
                centres[i] = members.mean(axis=0)
    return labels


def name_clusters(labels: Iterable[int], coords: np.ndarray,
                  items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per region: centroid, radius, and a human name.

    The name is the region's best-connected member. Term-frequency naming was
    tried first and produced fragments ("under / hours") — a side-effect of so
    many entity names being whole sentences — whereas the anchor entity is
    something a reader recognises.

    `items` are dicts with at least `label`, and optionally `weight` (degree)
    and `is_entity` (entities outrank documents as region anchors).
    """
    labels = np.asarray(list(labels), dtype=int)
    pts = np.asarray(coords, dtype=np.float64)
    out: list[dict[str, Any]] = []
    for cid in sorted({int(c) for c in labels} - {-1}):
        idx = np.flatnonzero(labels == cid)
        if not len(idx):
            continue
        anchor = max(idx, key=lambda i: (
            bool(items[i].get("is_entity", True)),
            float(items[i].get("weight", 0) or 0),
            -len(str(items[i].get("label", ""))),
        ))
        name = str(items[anchor].get("label") or f"region {cid}")
        if len(name) > 34:
            name = name[:32].rstrip() + "…"
        centre = pts[idx].mean(axis=0)
        radius = float(np.linalg.norm(pts[idx] - centre, axis=1).max()) if len(idx) > 1 else 0.0
        out.append({
            "id": int(cid),
            "name": name,
            "size": len(idx),
            "x": round(float(centre[0]), 2),
            "y": round(float(centre[1]), 2),
            "radius": round(max(radius, 40.0) + 34.0, 2),
        })
    return out


# entity type → fill colour. Deliberately the same palette graph.html already
# uses, so a node keeps its identity across the two views. 'untyped' is a muted
# slate rather than a real hue: unclassified entities should read as a gap in
# the data, not as another category.
TYPE_COLORS = {
    "agent": "#7aa2f7", "tool": "#9ece6a", "tech": "#e0af68",
    "concept": "#bb9af7", "project": "#bb9af7", "person": "#7dcfff",
    "other": "#f7768e", "untyped": "#565f73",
    "note": "#41a6b5", "document": "#41a6b5", "research": "#41a6b5",
    "summary": "#41a6b5",
}
DEFAULT_TYPE_COLOR = "#f7768e"


def build_map(entities: Iterable[Any], docs: Iterable[Any],
              doc_links: Iterable[Any] | None = None,
              clusters: Iterable[Any] | None = None) -> dict[str, Any]:
    """Cached rows → the document the /map page renders.

    Pure function over rows the caller fetched — no DB access, so it unit
    tests the same way `graph.build_graph` does.

    entities:  (id, name, etype, summary, project, map_x, map_y, map_cluster,
                fact_count, doclink_count)
    docs:      (id, title, original_filename, note_type, project, map_x, map_y,
                map_cluster, doclink_count)
    doc_links: (note_id, entity_id, score, method)
    clusters:  records produced by name_clusters()
    """
    points: list[dict[str, Any]] = []
    known: set[str] = set()

    for e in entities:
        r = dict(e)
        if r.get("map_x") is None or r.get("map_y") is None:
            continue
        pid = str(r["id"])
        known.add(pid)
        points.append({
            "id": pid,
            "label": r.get("name") or "(unnamed)",
            "ptype": "entity",
            "etype": (r.get("etype") or "untyped").lower(),
            "project": r.get("project") or "",
            "summary": (r.get("summary") or "")[:280],
            "facts": int(r.get("fact_count") or 0),
            "doclinks": int(r.get("doclink_count") or 0),
            "x": round(float(r["map_x"]), 2),
            "y": round(float(r["map_y"]), 2),
            "cluster": int(r["map_cluster"]) if r.get("map_cluster") is not None else -1,
        })

    for d in docs:
        r = dict(d)
        if r.get("map_x") is None or r.get("map_y") is None:
            continue
        note_type = r.get("note_type") or "note"
        label = (r.get("original_filename") if note_type == "document" else None) \
            or r.get("title") or "untitled"
        pid = f"doc:{r['id']}"
        known.add(pid)
        points.append({
            "id": pid,
            "label": label,
            "ptype": "doc",
            "etype": note_type,
            "project": r.get("project") or "",
            "summary": (r.get("snippet") or "")[:280],
            "facts": 0,
            "doclinks": int(r.get("doclink_count") or 0),
            "x": round(float(r["map_x"]), 2),
            "y": round(float(r["map_y"]), 2),
            "cluster": int(r["map_cluster"]) if r.get("map_cluster") is not None else -1,
        })

    links = []
    for dl in doc_links or []:
        r = dict(dl)
        src, dst = f"doc:{r['note_id']}", str(r["entity_id"])
        if src not in known or dst not in known:
            continue  # fell outside this query's limit
        links.append({
            "source": src, "target": dst,
            "score": round(float(r.get("score") or 0), 3),
            "method": r.get("method") or "embedding",
        })

    used = sorted({p["etype"] for p in points})
    return {
        "points": points,
        "links": links,
        "clusters": [dict(c) for c in (clusters or [])],
        "types": [{"name": t, "color": TYPE_COLORS.get(t, DEFAULT_TYPE_COLOR),
                   "count": sum(1 for p in points if p["etype"] == t)}
                  for t in used],
        "stats": {
            "entities": sum(1 for p in points if p["ptype"] == "entity"),
            "docs": sum(1 for p in points if p["ptype"] == "doc"),
            "links": len(links),
            "clusters": len(list(clusters or [])),
            "method": method(),
        },
    }


# ── DB side ───────────────────────────────────────────────────────────────────
# Everything above is pure; this is the one function that touches a connection,
# shared by the dashboard's /map page and the REST/MCP brain_map op so the query
# lives in exactly one place.

async def load_map(conn, *, project: str | None = None,
                   limit: int = 2000) -> dict[str, Any]:
    """Read cached coordinates and assemble the map document."""
    from .projects import aliases_of, canonical

    # Aggregate once in CTEs rather than running correlated subqueries per
    # entity: the correlated form costs O(entities x facts) and this is the
    # page most likely to be opened on a large brain.
    entities = await conn.fetch(
        """
        WITH fact_counts AS (
            SELECT subj AS eid, count(*) AS n FROM facts
            WHERE valid_to IS NULL GROUP BY subj
        ),
        doc_counts AS (
            SELECT entity_id AS eid, count(*) AS n FROM doc_links GROUP BY entity_id
        ),
        ent_proj AS (
            -- an entity can appear in facts from several projects; the one
            -- that produced the most facts about it wins, name breaking ties
            -- so the result is stable across runs
            SELECT DISTINCT ON (eid) eid, project FROM (
                SELECT f.subj AS eid, ev.project, count(*) AS c
                FROM facts f JOIN events ev ON ev.id = f.episode_id
                WHERE ev.project IS NOT NULL
                GROUP BY f.subj, ev.project
            ) s ORDER BY eid, c DESC, project
        )
        SELECT e.id, e.name, e.etype, e.summary, e.map_x, e.map_y, e.map_cluster,
               COALESCE(fc.n, 0) AS fact_count,
               COALESCE(dc.n, 0) AS doclink_count,
               ep.project
        FROM entities e
        LEFT JOIN fact_counts fc ON fc.eid = e.id
        LEFT JOIN doc_counts  dc ON dc.eid = e.id
        LEFT JOIN ent_proj    ep ON ep.eid = e.id
        WHERE e.map_x IS NOT NULL
        ORDER BY COALESCE(fc.n, 0) DESC, e.name
        LIMIT $1
        """,
        limit,
    )
    docs = await conn.fetch(
        """
        WITH doc_counts AS (
            SELECT note_id AS nid, count(*) AS n FROM doc_links GROUP BY note_id
        )
        SELECT n.id, n.title, n.original_filename, n.note_type, n.project,
               left(n.body, 280) AS snippet, n.map_x, n.map_y, n.map_cluster,
               COALESCE(dc.n, 0) AS doclink_count
        FROM notes n
        LEFT JOIN doc_counts dc ON dc.nid = n.id
        WHERE n.map_x IS NOT NULL
        ORDER BY n.ts DESC
        LIMIT $1
        """,
        limit,
    )

    # project lives on the event for entities and on the row for docs; both get
    # canonicalised here so the map groups `second-brain` with `cortex`.
    ent_rows = [{**dict(r), "project": canonical(r["project"])} for r in entities]
    doc_rows = [{**dict(r), "project": canonical(r["project"])} for r in docs]
    if project:
        wanted = set(aliases_of(project)) | {canonical(project)}
        ent_rows = [r for r in ent_rows if r["project"] in wanted]
        doc_rows = [r for r in doc_rows if r["project"] in wanted]

    note_ids = [r["id"] for r in doc_rows]
    links = await conn.fetch(
        "SELECT note_id, entity_id, score, method FROM doc_links "
        "WHERE note_id = ANY($1::uuid[])",
        note_ids,
    ) if note_ids else []
    clusters = await conn.fetch(
        "SELECT id, name, size, x, y, radius FROM map_clusters ORDER BY size DESC")

    doc = build_map(ent_rows, doc_rows, links, clusters)
    doc["projects"] = sorted({r["project"] for r in ent_rows + doc_rows if r["project"]})
    return doc
