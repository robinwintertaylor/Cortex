"""Unit tests for the graph builder (FR-13): nodes, edges, history, caps."""

from cortex.graph import build_graph


class Row(dict):
    """Fake asyncpg Record (dict-style access)."""
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


def entity(id, name, etype=None, summary=None, fact_count=1):
    return Row({"id": id, "name": name, "etype": etype, "summary": summary,
                "fact_count": fact_count})


def doc(id, title, note_type="note", project=None, tags=None, author=None,
        source_url=None, original_filename=None, links=None):
    return Row({"id": id, "title": title, "note_type": note_type,
                "project": project, "tags": tags or [], "author": author,
                "source_url": source_url, "original_filename": original_filename,
                "links": links or []})


def doc_link(note_id, entity_id, score=0.7, method=None):
    return Row({"note_id": note_id, "entity_id": entity_id, "score": score, "method": method})


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


def test_docs_become_doc_nodes():
    docs_ = [doc("d1", "Design notes", project="cortex")]
    g = build_graph([], [], docs=docs_)
    doc_nodes = [n for n in g["nodes"] if n["group"] == "doc"]
    assert len(doc_nodes) == 1
    assert doc_nodes[0]["label"] == "Design notes"


def test_document_note_type_labels_with_original_filename():
    docs_ = [doc("d1", "ignored title", note_type="document",
                 original_filename="spec.pdf")]
    g = build_graph([], [], docs=docs_)
    doc_node = next(n for n in g["nodes"] if n["group"] == "doc")
    assert doc_node["label"] == "spec.pdf"


def test_doc_links_to_matching_project_entity():
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Design notes", project="cortex", author="dsh")]
    g = build_graph(ents, [], docs=docs_)
    doc_id = next(n["id"] for n in g["nodes"] if n["group"] == "doc")
    assert len(g["edges"]) == 1
    e = g["edges"][0]
    assert e["from"] == doc_id and e["to"] == "u1"
    assert e["label"] == "project"
    assert e["kind"] == "doc"
    assert e["harness"] == "dsh"


def test_doc_links_to_matching_tag_case_insensitively():
    ents = [entity("u1", "Postgres", "tech")]
    docs_ = [doc("d1", "Migration guide", tags=["postgres", "ops"])]
    g = build_graph(ents, [], docs=docs_)
    assert len(g["edges"]) == 1
    assert g["edges"][0]["label"] == "references"


def test_doc_with_no_matches_is_still_a_standalone_node():
    docs_ = [doc("d1", "Orphan note")]
    g = build_graph([], [], docs=docs_)
    assert len(g["nodes"]) == 1
    assert len(g["edges"]) == 0


def test_doc_links_embedding_match_to_orphan_doc():
    """The librarian's semantic-similarity pass connects a doc that has no
    project/tags/links overlap with any entity at all."""
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Orphan note")]  # no project/tags/links
    links_ = [doc_link("d1", "u1", score=0.71)]
    g = build_graph(ents, [], docs=docs_, doc_links=links_)
    assert len(g["edges"]) == 1
    e = g["edges"][0]
    assert e["label"] == "related_to"
    assert e["kind"] == "doc"
    assert e["harness"] == "auto-link"
    assert "score=0.71" in e["title"]


def test_doc_links_dedupe_against_string_match_edge():
    """An embedding match landing on the same entity a project/tags/links
    match already reached must not produce a second edge."""
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Design notes", project="cortex", author="dsh")]
    links_ = [doc_link("d1", "u1", score=0.9)]
    g = build_graph(ents, [], docs=docs_, doc_links=links_)
    assert len(g["edges"]) == 1
    assert g["edges"][0]["label"] == "project"  # the string match, not the embed one


def test_doc_links_method_defaults_to_embedding_in_title():
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Orphan note")]
    g = build_graph(ents, [], docs=docs_, doc_links=[doc_link("d1", "u1")])
    assert "method=embedding" in g["edges"][0]["title"]


def test_doc_links_llm_method_appears_in_title():
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Orphan note")]
    g = build_graph(ents, [], docs=docs_, doc_links=[doc_link("d1", "u1", method="llm")])
    assert "method=llm" in g["edges"][0]["title"]


def test_doc_links_ignore_entities_outside_the_query_window():
    """An entity_id doc_links points at but that isn't in this render's
    entity set (paged out by LIMIT) must not crash or dangle an edge."""
    docs_ = [doc("d1", "Orphan note")]
    links_ = [doc_link("d1", "missing-entity", score=0.8)]
    g = build_graph([], [], docs=docs_, doc_links=links_)
    assert len(g["edges"]) == 0


def test_doc_kind_and_harness_appear_in_legends():
    ents = [entity("u1", "cortex", "project")]
    docs_ = [doc("d1", "Design notes", project="cortex", author="goose")]
    g = build_graph(ents, [], docs=docs_)
    assert {k["name"] for k in g["kinds"]} == {"doc"}
    assert {h["name"] for h in g["harnesses"]} == {"goose"}


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
