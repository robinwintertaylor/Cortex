"""Graph view builder (FR-13 dashboard): entities + facts → nodes + edges.

Pure function over rows fetched by the caller — unit-tested without a DB.
Nodes are entities (grouped by entity type: agent, tool, tech, project,
concept, person, …) plus literal object nodes for facts whose object is a
plain value. Edges are facts (predicate as label); superseded facts render
dashed with the validity window in the title. Designed for vis-network but
emits plain JSON — usable from the REST API and MCP too.
"""

from __future__ import annotations

from typing import Any, Iterable

# entity types get distinct colors in the UI; unknown types fall back
KNOWN_TYPES = ("agent", "tool", "tech", "concept", "project", "person", "other")


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
        edges.append({
            "id": str(f["id"]),
            "from": s_node["id"],
            "to": o_node["id"],
            "label": f["pred"],
            "title": (f"{f['subj_name']} — {f['pred']} → "
                      f"{(f['obj_text'] or '').strip() or '(entity)'}"
                      f"\nkind={f['kind']} conf={f['confidence']:.2f}"
                      f"\nvalid_from={_iso(f['valid_from'])}"
                      + (f"\nsuperseded={_iso(f['valid_to'])}" if superseded else "")),
            "dashes": superseded,
            "font": {"size": 9, "align": "middle"},
        })

    # cap: keep highest-degree nodes
    if len(nodes) > max_nodes:
        keep = set(sorted(nodes, key=lambda k: -nodes[k]["value"])[:max_nodes])
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges
                 if e["from"] in keep and e["to"] in keep]

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {"entities": len(nodes), "facts": len(edges)},
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
