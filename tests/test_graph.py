"""Unit tests for the graph builder (FR-13): nodes, edges, history, caps."""

from cortex.graph import build_graph


class Row(dict):
    """Fake asyncpg Record (dict-style access)."""
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


def entity(id, name, etype=None, summary=None, fact_count=1):
    return Row({"id": id, "name": name, "etype": etype, "summary": summary,
                "fact_count": fact_count})


def fact(id, subj, subj_name, pred, obj_text, obj=None, valid_to=None,
         kind="extracted", confidence=0.8, harness=None, agent=None):
    from datetime import datetime, timezone

    return Row({"id": id, "subj": subj, "subj_name": subj_name, "pred": pred,
                "obj": obj, "obj_text": obj_text, "valid_from":
                datetime.now(timezone.utc), "valid_to": valid_to,
                "kind": kind, "confidence": confidence,
                "harness": harness, "agent": agent})


def test_entities_become_nodes():
    g = build_graph([entity("u1", "cortex", "project")], [])
    assert len(g["nodes"]) == 1
    assert g["nodes"][0]["group"] == "project"


def test_fact_links_entities_and_literal_objects():
    ents = [entity("u1", "cortex", "project"), entity("u2", "postgres", "tech")]
    facts_ = [fact("f1", "u1", "cortex", "uses_database", "postgres 16")]
    g = build_graph(ents, facts_)
    # three nodes: two entities + one literal value node for the object text
    assert len(g["nodes"]) == 3
    assert len(g["edges"]) == 1
    e = g["edges"][0]
    assert e["from"] == "u1" and e["label"] == "uses_database"
    assert e["to"].startswith("lit:")  # literal object node


def test_fact_with_entity_object_links_directly():
    ents = [entity("u1", "dsh", "agent"), entity("u2", "Write", "tool")]
    facts_ = [fact("f1", "u1", "dsh", "uses_tool", "Write", obj="u2")]
    g = build_graph(ents, facts_)
    assert len(g["nodes"]) == 2
    assert g["edges"][0]["to"] == "u2"


def test_harness_ids_grouped_as_agents():
    facts_ = [fact("f1", None, "claude-code", "uses_tool", "Edit")]
    g = build_graph([], facts_)
    subj = [n for n in g["nodes"] if n["label"] == "claude-code"]
    assert subj and subj[0]["group"] == "agent"


def test_history_flag_controls_superseded_edges():
    from datetime import datetime, timezone

    ents = [entity("u1", "cortex", "project")]
    old = fact("f1", "u1", "cortex", "uses_db", "sqlite",
               valid_to=datetime.now(timezone.utc))
    new = fact("f2", "u1", "cortex", "uses_db", "postgres")
    g = build_graph(ents, [old, new])  # history off by default
    assert all(not e["dashes"] for e in g["edges"])
    assert len(g["edges"]) == 1
    g = build_graph(ents, [old, new], include_history=True)
    assert len(g["edges"]) == 2
    assert sum(e["dashes"] for e in g["edges"]) == 1
    assert "superseded=" in [e for e in g["edges"] if e["dashes"]][0]["title"]


def test_max_nodes_cap_prunes_low_degree():
    ents = [entity(f"u{i}", f"e{i}", "concept", fact_count=1) for i in range(10)]
    facts_ = [fact("f1", "u0", "e0", "rel", "x", obj=None)]
    g = build_graph(ents, facts_, max_nodes=5)
    assert len(g["nodes"]) <= 6  # 5 kept + possibly the literal target
    assert all(e["from"] in {n["id"] for n in g["nodes"]} for e in g["edges"])


def test_edges_carry_kind_and_harness_with_fallback():
    """Provenance travels on the edge: prefer harness, fall back to agent,
    then 'unknown' for facts with no episode join at all."""
    ents = [entity("u1", "cortex", "project")]
    facts_ = [
        fact("f1", "u1", "cortex", "uses_db", "postgres", kind="decided", harness="dsh", agent="dsh"),
        fact("f2", "u1", "cortex", "uses_tool", "Write", kind="observed", agent="claude-code"),
        fact("f3", "u1", "cortex", "runs_on", "docker"),  # no harness/agent at all
    ]
    g = build_graph(ents, facts_)
    by_pred = {e["label"]: e for e in g["edges"]}
    assert by_pred["uses_db"]["harness"] == "dsh"
    assert by_pred["uses_db"]["kind"] == "decided"
    assert by_pred["uses_tool"]["harness"] == "claude-code"  # falls back to agent
    assert by_pred["runs_on"]["harness"] == "unknown"


def test_node_contributors_reflect_harness_share():
    """Each node's contributors ring is the harness breakdown of the facts
    that touch it, proportioned by share."""
    ents = [entity("u1", "cortex", "project")]
    facts_ = [
        fact("f1", "u1", "cortex", "a", "x", harness="dsh"),
        fact("f2", "u1", "cortex", "b", "y", harness="dsh"),
        fact("f3", "u1", "cortex", "c", "z", harness="claude-code"),
    ]
    g = build_graph(ents, facts_)
    node = next(n for n in g["nodes"] if n["label"] == "cortex")
    by_harness = {c["harness"]: c for c in node["contributors"]}
    assert by_harness["dsh"]["count"] == 2
    assert by_harness["dsh"]["share"] == 2 / 3
    assert by_harness["claude-code"]["count"] == 1


def test_harnesses_and_kinds_summarize_surviving_edges_only():
    """The top-level harness/kind legends reflect what's actually in the
    (possibly history-filtered) edge set, not every fact ever passed in."""
    ents = [entity("u1", "cortex", "project")]
    superseded = fact("f1", "u1", "cortex", "old", "v1", kind="lesson",
                      harness="goose", valid_to=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    current = fact("f2", "u1", "cortex", "new", "v2", kind="decided", harness="dsh")
    g = build_graph(ents, [superseded, current])  # history off by default
    assert {h["name"] for h in g["harnesses"]} == {"dsh"}
    assert {k["name"] for k in g["kinds"]} == {"decided"}
    g_hist = build_graph(ents, [superseded, current], include_history=True)
    assert {h["name"] for h in g_hist["harnesses"]} == {"dsh", "goose"}
    assert {k["name"] for k in g_hist["kinds"]} == {"decided", "lesson"}
