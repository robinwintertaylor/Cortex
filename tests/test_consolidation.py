"""Unit tests for the consolidation decision (FR-5) — the core brain logic."""

from cortex.librarian.consolidation import (
    Action,
    ExistingFact,
    NewFact,
    classify,
    objects_equal,
    objects_similar,
)


def fact(obj, kind="extracted", id="f1", subj="cortex", pred="uses_database"):
    return ExistingFact(id=id, subj_name=subj, pred=pred, obj_text=obj, kind=kind)


def new(obj, kind="note", role="agent", subj="cortex", pred="uses_database"):
    return NewFact(subj_name=subj, pred=pred, obj_text=obj, event_kind=kind, agent_role=role)


def test_no_existing_fact_adds():
    assert classify(new("postgres"), []) is Action.ADD


def test_same_object_noop():
    ex = [fact("postgres 16")]
    assert classify(new("postgres 16", kind="note"), ex) is Action.NOOP


def test_noop_is_case_and_whitespace_insensitive():
    ex = [fact("Postgres  16")]
    assert classify(new("postgres 16"), ex) is Action.NOOP


def test_refined_wording_is_noop_not_supersede():
    ex = [fact("postgres 16")]
    assert classify(new("postgres 16 database", kind="note"), ex) is Action.NOOP


def test_changed_object_non_authoritative_adjudicates():
    """FR-5 AC4: a contradicting fact lands in the queue, never overwrites."""
    ex = [fact("postgres 16")]
    assert classify(new("mysql 8"), ex) is Action.ADJUDICATE


def test_changed_object_authoritative_supersedes():
    ex = [fact("postgres 16", kind="decided")]
    # decision/lesson events are authoritative → SUPERSEDE
    assert classify(new("qdrant", kind="decision"), ex) is Action.SUPERSEDE
    assert classify(new("qdrant", kind="lesson"), ex) is Action.SUPERSEDE
    # also authoritative via agent role
    assert classify(new("qdrant", role="owner"), ex) is Action.SUPERSEDE
    assert classify(new("qdrant", role="admin"), ex) is Action.SUPERSEDE


def test_supersede_thrash_guard():
    ex = [fact("postgres 16")]
    n = new("qdrant", kind="decision")
    assert classify(n, ex, supersessions_today=5) is Action.ADJUDICATE
    assert classify(n, ex, supersessions_today=4) is Action.SUPERSEDE


def test_predicate_mismatch_adds():
    ex = [fact("postgres 16", pred="uses_database")]
    assert classify(new("fast", pred="runs_on"), ex) is Action.ADD


def test_subject_mismatch_adds():
    ex = [fact("postgres 16", subj="other-project")]
    assert classify(new("mysql", subj="cortex"), ex) is Action.ADD


def test_objects_helpers():
    assert objects_equal("PG 16", "pg 16")
    assert not objects_equal("pg 16", "pg 17")
    assert objects_similar("pg 16", "pg 16 server")
    assert not objects_similar("postgres 16", "mysql 8")
