"""Project-name canonicalisation.

Project labels arrive from whichever harness happened to write the event, so the
same project accumulates spellings — `Bletchley Broadcast` / `bletchley-broadcast`,
or `cortex` / `cortex-setup` / `second-brain` / `it-cortex`. Six labels for two
projects splits every filter, every project cluster on the map, and every
per-project digest.

`events` is append-only, so historical rows keep whatever they were written with
— forever. Canonicalisation therefore happens on two sides:

  * **write** — `events.append` and the projection writers normalise before
    storing, so new rows are consistent;
  * **read**  — `aliases_of()` expands a canonical name back to every spelling
    it has ever had, so a query for `cortex` still finds rows stored as
    `second-brain`.

Override the defaults with CORTEX_PROJECT_ALIASES as a JSON object mapping
alias → canonical name.
"""

from __future__ import annotations

import json
import os

DEFAULT_ALIASES: dict[str, str] = {
    "bletchley-broadcast": "Bletchley Broadcast",
    "cortex-setup": "cortex",
    "second-brain": "cortex",
    "it-cortex": "cortex",
}


def _aliases() -> dict[str, str]:
    raw = os.environ.get("CORTEX_PROJECT_ALIASES")
    if not raw:
        return DEFAULT_ALIASES
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return DEFAULT_ALIASES
    if not isinstance(loaded, dict):
        return DEFAULT_ALIASES
    return {str(k).strip().lower(): str(v) for k, v in loaded.items()}


def canonical(project: str | None) -> str | None:
    """Map any known spelling of a project to its canonical name."""
    if not project:
        return project
    return _aliases().get(project.strip().lower(), project)


def aliases_of(project: str | None) -> list[str]:
    """Every stored spelling matching `project` — for read-time filtering.

    Returns the input itself plus any alias that canonicalises to the same
    name, so `project = ANY($n)` finds historical rows the log still holds
    under an old label.
    """
    if not project:
        return []
    target = canonical(project)
    found = {project, target} if target else {project}
    found.update(alias for alias, canon in _aliases().items() if canon == target)
    return sorted(found)
