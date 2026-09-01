"""Librarian consolidation pass (FR-5): match each extracted fact against
existing same-predicate facts → ADD / NOOP / SUPERSEDE / ADJUDICATE.

The decision function is pure (unit-tested); the DB-touching executor lives in
the worker. A contradicting fact from a non-authoritative source lands in the
adjudication queue — never a silent overwrite (AC4). Supersession thrash is
bounded by a per-(subject, predicate) daily rate limit (risk table)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Action(str, Enum):
    ADD = "ADD"
    NOOP = "NOOP"
    SUPERSEDE = "SUPERSEDE"
    ADJUDICATE = "ADJUDICATE"


# kinds of events that speak with authority (PRD FR-5: "changed object from an
# authoritative event → SUPERSEDE"); decisions/lessons are deliberate acts.
AUTHORITATIVE_KINDS = {"decision", "lesson"}
AUTHORITATIVE_ROLES = {"owner", "admin"}

# consolidation-thrash guard (risk table: "rate-limit supersessions per predicate/day")
SUPERSEDE_DAILY_LIMIT = 5

NOOP_CONFIDENCE_BUMP = 0.05


def _norm(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def objects_equal(a: str | None, b: str | None) -> bool:
    return _norm(a) == _norm(b) and _norm(a) != ""


def objects_similar(a: str | None, b: str | None, threshold: float = 0.6) -> bool:
    """Conservative token-overlap similarity for 'same fact, refined wording'.
    Jaccard ≥ 0.6 (e.g. 'pg 16' vs 'pg 16 server' = 0.67 → similar; but
    'postgres 16' vs 'postgres 17' = 0.5 → changed, a real supersession)."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


@dataclass
class ExistingFact:
    """Minimal view of a current (valid_to IS NULL) fact row."""
    id: str
    subj_name: str
    pred: str
    obj_text: str | None
    kind: str


@dataclass
class NewFact:
    subj_name: str
    pred: str
    obj_text: str | None
    event_kind: str            # kind of the producing event
    agent_role: str = "agent"  # resolved role of the producing agent


def classify(new: NewFact, existing: list[ExistingFact],
             supersessions_today: int = 0) -> Action:
    """The core consolidation decision (pure; FR-5 AC4, thrash guard).

    - no current fact for (subject, predicate)        → ADD
    - current fact with the same object               → NOOP (bump confidence)
    - changed object, authoritative source            → SUPERSEDE
      (subject to the daily thrash limit)
    - changed object, non-authoritative source        → ADJUDICATE
    """
    same_pred = [e for e in existing
                 if _norm(e.pred) == _norm(new.pred)
                 and _norm(e.subj_name) == _norm(new.subj_name)]
    if not same_pred:
        return Action.ADD
    for e in same_pred:
        if objects_equal(e.obj_text, new.obj_text):
            return Action.NOOP
        if objects_similar(e.obj_text, new.obj_text):
            return Action.NOOP  # refinement of the same fact — not a change
    # changed object (or object removed) → who says so?
    authoritative = new.event_kind in AUTHORITATIVE_KINDS or new.agent_role in AUTHORITATIVE_ROLES
    if authoritative and supersessions_today < SUPERSEDE_DAILY_LIMIT:
        return Action.SUPERSEDE
    return Action.ADJUDICATE
