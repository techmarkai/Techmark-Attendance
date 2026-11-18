"""Event classification. Pure logic, no I/O.

The only library the integration knows about is the Employee Library. A match
against any other library and a face that matched nothing are treated
identically: both are anonymous non-employees. There is therefore no branch
anywhere that depends on a second library's identifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import (
    CONFLICT_DUPLICATE_IDENTITY,
    CONFLICT_NO_IDENTIFIER,
    KIND_EMPLOYEE_MATCH,
    KIND_NON_EMPLOYEE,
    LIBRARY_NAME_EMPLOYEE,
)
from .models import FaceMatch


@dataclass(frozen=True, slots=True)
class EmployeeLibrary:
    """Which face library holds employees.

    Discovered from the camera's library names at setup and overridable in
    configuration. One value is all the integration needs.
    """

    fdid: str | None = None
    name: str = LIBRARY_NAME_EMPLOYEE

    @classmethod
    def from_libraries(cls, libraries: list[dict[str, Any]]) -> EmployeeLibrary:
        """Find the Employee Library by name. Other libraries are ignored."""
        wanted = LIBRARY_NAME_EMPLOYEE.casefold()
        for lib in libraries:
            if str(lib.get("name") or "").strip().casefold() == wanted:
                fdid = str(lib.get("fdid") or "").strip()
                if fdid:
                    return cls(fdid=fdid, name=str(lib.get("name")))
        return cls()

    def is_employee(self, match: FaceMatch) -> bool:
        """True only for a match against the Employee Library.

        Matched on the library identifier when known, falling back to the
        library name the camera reported with the event. A display name is
        never the basis of this decision.
        """
        if self.fdid and match.fdid:
            return match.fdid == self.fdid
        return match.library.strip().casefold() == self.name.strip().casefold()

    def kind_for(self, match: FaceMatch) -> str:
        return KIND_EMPLOYEE_MATCH if self.is_employee(match) else KIND_NON_EMPLOYEE


def employee_key(person_key: str, name: str) -> str:
    """Stable employee identity.

    `customHumanID` is preferred, but the live Employee record carries an empty
    one, so the library name is the documented fallback. The `name:` prefix
    keeps the two namespaces apart so one person cannot be counted under both
    forms. A display-name change updates a label, never the identity, as long
    as a `customHumanID` exists.
    """
    person_key = (person_key or "").strip()
    if person_key:
        return person_key
    name = (name or "").strip()
    return f"name:{name}" if name else ""


def passes_quality(quality: int | None, minimum: int) -> bool:
    """Quality gate for UNMATCHED faces only.

    Matches are gated by the camera's own library threshold on similarity, not
    by the quality score: observed correct matches scored 49-58 against a
    library threshold of 80, so a quality floor would discard genuine
    identifications.
    """
    if quality is None:
        return True
    return quality >= minimum


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One employee resolved from the Employee Library."""

    key: str
    name: str
    pid: str


@dataclass(frozen=True, slots=True)
class RosterConflict:
    """A library record whose identity could not be resolved cleanly.

    Surfaced rather than guessed: two people must never be silently merged
    into one employee.
    """

    reason: str
    key: str
    pids: tuple[str, ...]
    names: tuple[str, ...]


def resolve_roster(
    records: list[dict[str, str]],
) -> tuple[list[RosterEntry], list[RosterConflict]]:
    """Turn Employee Library records into a roster, reporting any conflict.

    Identity comes from `customHumanID`, falling back to the library name
    (prefixed, see `employee_key`). Two cases cannot be resolved and are
    reported instead of guessed:

    * `no_identifier` - the record has neither an id nor a name, so it cannot
      be addressed at all and is left out of the roster.
    * `duplicate_identity` - two records resolve to the same key. The first is
      kept so attendance still works for it, and the clash is reported so it
      is visible rather than silently merged.
    """
    entries: list[RosterEntry] = []
    conflicts: list[RosterConflict] = []
    seen: dict[str, RosterEntry] = {}

    for record in records:
        name = str(record.get("name") or "").strip()
        pid = str(record.get("pid") or "").strip()
        key = employee_key(str(record.get("human_id") or ""), name)

        if not key:
            conflicts.append(
                RosterConflict(CONFLICT_NO_IDENTIFIER, "", (pid,), (name,))
            )
            continue

        if key in seen:
            first = seen[key]
            conflicts.append(
                RosterConflict(
                    CONFLICT_DUPLICATE_IDENTITY,
                    key,
                    (first.pid, pid),
                    (first.name, name),
                )
            )
            continue

        entry = RosterEntry(key=key, name=name or key, pid=pid)
        seen[key] = entry
        entries.append(entry)

    return entries, conflicts
