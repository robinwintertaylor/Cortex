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

# entity types get distinct fill colors in the UI; unknown types fall back
KNOWN_TYPES = ("agent", "tool", "tech", "concept", "project", "person", "other")

# fact.kind → edge color. Chosen not to collide with KNOWN_TYPES fill colors
# (see graph.html) so "kind" (edge color) and "etype" (node fill) read as
# separate channels at a glance.
KIND_COLORS = {
    "decided": "#e0af68",   # gold — a decision was made
    "lesson": "#bb9af7",    # violet — a durable lesson
    "owner": "#7dcfff",     # cyan — an ownership/assignment fact
    "extracted": "#9ece6a", # green — LLM-extracted from an episode
    "observed": "#565f73",  # muted slate — raw auto-captured tool-use
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
                include_history: bool = False,
                max_nodes: int = 300) -> dict[str, Any]:
    """Build a vis-network-shaped {nodes, edges} document.

    entities: rows with (id, name, etype, summary)
    facts:    rows with (id, subj, subj_name, pred, obj, obj_text,
                         valid_from, valid_to, kind, confidence)
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
