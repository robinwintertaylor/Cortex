"""Graph view builder (FR-13 dashboard): entities + facts → nodes + edges.

Pure function over rows fetched by the caller — unit-tested without a DB.
Nodes are entities (grouped by entity type: agent, tool, tech, project,
concept, person, …) plus literal object nodes for facts whose object is a
plain value. Edges are facts (predicate as label); superseded facts render
dashed with the validity window in the title. Designed for vis-network but
emits plain JSON — usable from the REST API and MCP too.

Provenance: each fact carries the harness/agent that logged it (via
events.harness/agent, joined in by the caller). Edges are colored by fact
`kind` (what the information IS — decided/lesson/owner/extracted/observed);
each node aggregates a `contributors` breakdown (which harnesses produced
facts touching it) so the UI can render a per-node provenance ring — the
"who said this" answer, distinct from "what kind of thing this is".
"""

from __future__ import annotations

from typing import Any, Iterable

# entity types get distinct fill colors in the UI; unknown types fall back.
# "doc" is synthetic — it's notes/uploaded files (cortex/files.py), not an
# entity-table row, but it renders as a node group like any other.
KNOWN_TYPES = ("agent", "tool", "tech", "concept", "project", "person", "doc", "other")

# fact.kind → edge color. Chosen not to collide with KNOWN_TYPES fill colors
# (see graph.html) so "kind" (edge color) and "etype" (node fill) read as
# separate channels at a glance.
KIND_COLORS = {
    "decided": "#e0af68",   # gold — a decision was made
    "lesson": "#bb9af7",    # violet — a durable lesson
    "owner": "#7dcfff",     # cyan — an ownership/assignment fact
    "extracted": "#9ece6a", # green — LLM-extracted from an episode
    "observed": "#565f73",  # muted slate — raw auto-captured tool-use
    "doc": "#41a6b5",       # teal — a doc/file referencing an entity
}
DEFAULT_KIND_COLOR = "#565f73"

# stable, curated palette for harness/agent identity (the provenance ring +
# legend). Picked to stay visually distinct from KNOWN_TYPES fill colors and
# KIND_COLORS above; cycles if more harnesses show up than swatches.
HARNESS_PALETTE = [
    "#ff9e64", "#73daca", "#2ac3de", "#c0caf5",
    "#ff007c", "#a9b1d6", "#b4f9f8", "#89ddff",
]


def build_graph(entities: Iterable[Any], facts: Iterable[Any], *,
                docs: Iterable[Any] | None = None,
                doc_links: Iterable[Any] | None = None,
                include_history: bool = False,
                max_nodes: int = 300) -> dict[str, Any]:
    """Build a vis-network-shaped {nodes, edges} document.

    entities:  rows with (id, name, etype, summary)
    facts:     rows with (id, subj, subj_name, pred, obj, obj_text,
                          valid_from, valid_to, kind, confidence)
    docs:      rows with (id, title, note_type, project, tags, author,
                          source_url, original_filename, links) — project docs
                          and uploaded files (cortex/notes.py, cortex/files.py).
                          Each becomes a 'doc' node; it's edged to any entity
                          whose name matches the doc's project/tags/links
                          (case-insensitively) so docs slot into the same
                          graph their content is about.
    doc_links: rows with (note_id, entity_id, score, method) — matches found
                          by librarian.worker's embedding-similarity pass and
                          optional LLM pass (cortex/librarian/worker.py), for
                          docs the project/tags/links match above missed
                          entirely. Edged as 'related_to' under the synthetic
                          'auto-link' harness so they're visually and
                          filterably distinct from a human/agent assertion;
                          `method` ('embedding'|'llm') travels into the edge
                          title only, not a separate visual channel.
    Literal objects (obj_text without an entity) become 'value' nodes.
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node_for(uuid, name, group, title) -> dict | None:
        if uuid is None:
            return None
        if str(uuid) not in nodes:
            n = {
                "id": str(uuid),
                "label": str(name),
                "group": group if group in KNOWN_TYPES else "other",
                "title": title or str(name),
                "value": 1,
            }
            nodes[str(uuid)] = n
        return nodes[str(uuid)]

    for e in entities:
        node_for(getattr(e, "id", None) or e["id"], e["name"],
                 (e["etype"] or "other").lower(), e["summary"])

    # entity rows may carry fact_count → use it for node sizing
    for e in entities:
        fc = dict(e).get("fact_count") if hasattr(e, "keys") else None
        if fc and str(e["id"]) in nodes:
            nodes[str(e["id"])]["value"] = max(1, int(fc))

    for f in facts:
        superseded = f["valid_to"] is not None
        if superseded and not include_history:
            continue
        # subject node (entity uuid preferred, else a synthetic node keyed
        # by the subject's name so same-named subjects merge)
        subj_key = str(f["subj"]) if f["subj"] else f"subj:{_norm_name(f['subj_name'])}"
        s_node = node_for(f["subj"], f["subj_name"],
                          _guess_group(f["subj_name"]), None) \
            or _synthetic(subj_key, f["subj_name"], _guess_group(f["subj_name"]), nodes)
        # object node: linked entity, or literal value
        if f["obj"]:
            o_node = node_for(f["obj"], (f["obj_text"] or "").strip() or str(f["obj"]),
                              "other", None) \
                or _synthetic(str(f["obj"]), str(f["obj"]), "other", nodes)
        else:
            obj_label = (f["obj_text"] or "").strip()
            if not obj_label:
                continue
            obj_key = f"lit:{_norm_name(obj_label)}"
            o_node = _synthetic(obj_key, obj_label, "value", nodes)
        kind = f["kind"]
        # provenance: the caller left-joins events so harness/agent travel
        # alongside the fact row; older/backfilled facts may have neither.
        harness = f.get("harness") or f.get("agent") or "unknown"
        edges.append({
            "id": str(f["id"]),
            "from": s_node["id"],
            "to": o_node["id"],
            "label": f["pred"],
            "title": (f"{f['subj_name']} — {f['pred']} → "
                      f"{(f['obj_text'] or '').strip() or '(entity)'}"
                      f"\nkind={kind} conf={f['confidence']:.2f}"
                      f"\nharness={harness}"
                      f"\nvalid_from={_iso(f['valid_from'])}"
                      + (f"\nsuperseded={_iso(f['valid_to'])}" if superseded else "")),
            "dashes": superseded,
            "font": {"size": 9, "align": "middle"},
            "kind": kind,
            "harness": harness,
            "agent": f.get("agent") or harness,
            "color": {"color": KIND_COLORS.get(kind, DEFAULT_KIND_COLOR)},
        })

    # name → node id, so docs can be edged to entities/subjects by name
    # (docs have no fact rows connecting them; project/tags/links are the
    # only signal we have of what a doc is "about").
    name_index: dict[str, str] = {}
    for nid, n in nodes.items():
        name_index.setdefault(_norm_name(n["label"]), nid)

    # doc_id -> entity node ids already edged to it (any source), so the
    # embedding-linking pass below doesn't duplicate a project/tags/links
    # match that happens to land on the same entity.
    doc_linked: dict[str, set[str]] = {}

    for d in docs or []:
        row = dict(d) if hasattr(d, "keys") else d
        note_type = row.get("note_type") or "note"
        label = (row.get("original_filename") if note_type == "document" else None) \
            or row.get("title") or "untitled"
        doc_id = f"doc:{row['id']}"
        title_bits = [f"[{note_type}] {label}"]
        if row.get("project"):
            title_bits.append(f"project={row['project']}")
        if row.get("tags"):
            title_bits.append(f"tags={', '.join(row['tags'])}")
        if row.get("source_url"):
            title_bits.append(f"source={row['source_url']}")
        nodes[doc_id] = {
            "id": doc_id, "label": str(label), "group": "doc",
            "title": "\n".join(title_bits), "value": 1,
        }
        candidates = [row.get("project")] + list(row.get("tags") or []) \
            + list(row.get("links") or [])
        author = row.get("author") or "unknown"
        linked = doc_linked.setdefault(doc_id, set())
        for cand in candidates:
            if not cand:
                continue
            target_id = name_index.get(_norm_name(cand))
            if not target_id or target_id == doc_id or target_id in linked:
                continue
            linked.add(target_id)
            edges.append({
                "id": f"docref:{row['id']}:{target_id}",
                "from": doc_id, "to": target_id,
                "label": "project" if cand == row.get("project") else "references",
                "title": f"{label} references {cand}\nkind=doc\nharness={author}",
                "dashes": False,
                "font": {"size": 9, "align": "middle"},
                "kind": "doc",
                "harness": author,
                "agent": author,
                "color": {"color": KIND_COLORS["doc"]},
            })

    # embedding-discovered doc↔entity links (librarian.worker) — same 'doc'
    # kind/color, but labeled + attributed distinctly since no human/agent
    # actually asserted this connection, a similarity score did.
    for dl in doc_links or []:
        row = dict(dl) if hasattr(dl, "keys") else dl
        doc_id = f"doc:{row['note_id']}"
        target_id = str(row["entity_id"])
        if doc_id not in nodes or target_id not in nodes:
            continue  # doc or entity fell outside this query's LIMIT
        linked = doc_linked.setdefault(doc_id, set())
        if target_id == doc_id or target_id in linked:
            continue
        linked.add(target_id)
        score = row.get("score")
        method = row.get("method") or "embedding"
        edges.append({
            "id": f"doclink:{row['note_id']}:{target_id}",
            "from": doc_id, "to": target_id,
            "label": "related_to",
            "title": (f"{nodes[doc_id]['label']} related_to {nodes[target_id]['label']}"
                      f"\nkind=doc\nharness=auto-link\nmethod={method}"
                      + (f"\nscore={score:.2f}" if score is not None else "")),
            "dashes": False,
            "font": {"size": 9, "align": "middle"},
            "kind": "doc",
            "harness": "auto-link",
            "agent": "auto-link",
            "color": {"color": KIND_COLORS["doc"]},
        })

    # cap: keep highest-degree nodes
    if len(nodes) > max_nodes:
        keep = set(sorted(nodes, key=lambda k: -nodes[k]["value"])[:max_nodes])
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges
                 if e["from"] in keep and e["to"] in keep]

    # provenance rings: for each surviving node, which harnesses produced
    # the (surviving) facts touching it, and in what proportion.
    harnesses_used = sorted({e["harness"] for e in edges})
    kinds_used = sorted({e["kind"] for e in edges})
    harness_colors = {h: HARNESS_PALETTE[i % len(HARNESS_PALETTE)]
                      for i, h in enumerate(harnesses_used)}
    contrib: dict[str, dict[str, int]] = {}
    for e in edges:
        for end in (e["from"], e["to"]):
            d = contrib.setdefault(end, {})
            d[e["harness"]] = d.get(e["harness"], 0) + 1
    for nid, node in nodes.items():
        c = contrib.get(nid, {})
        total = sum(c.values())
        node["contributors"] = [
            {"harness": h, "count": cnt, "share": cnt / total,
             "color": harness_colors.get(h, DEFAULT_KIND_COLOR)}
            for h, cnt in sorted(c.items(), key=lambda kv: -kv[1])
        ] if total else []

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {"entities": len(nodes), "facts": len(edges)},
        "harnesses": [{"name": h, "color": harness_colors[h]} for h in harnesses_used],
        "kinds": [{"name": k, "color": KIND_COLORS.get(k, DEFAULT_KIND_COLOR)} for k in kinds_used],
    }


def _synthetic(key: str, label: str, group: str, nodes: dict) -> dict:
    if key not in nodes:
        nodes[key] = {"id": key, "label": str(label), "group": group,
                      "title": str(label), "value": 1}
    return nodes[key]


def _guess_group(name: str) -> str:
    """Agents appear as subjects of tool facts; classify common harness ids."""
    n = (name or "").lower()
    if n in ("dsh", "goose", "vibe", "claude-code", "claude-desktop", "buzz",
             "sweep", "hook", "hooks", "cortex"):
        return "agent"
    return "other"


def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else str(v)
