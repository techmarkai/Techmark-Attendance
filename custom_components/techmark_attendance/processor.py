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


def employee_key(record_id: str, name: str) -> str:
    """Stable employee identity. Both code paths MUST call this.

    The identity is the camera's own library record id: `PID` as `FDSearch`
    reports it, `human_id` as the alert stream reports it. Verified on the live
    camera: an event carrying `human_id='1'` matched `PID 1`, Yousef.

    `customHumanID` is deliberately NOT used, even though `FDSearch` returns
    it. A face comparison result does not contain that field at all, so keying
    on it would work for the roster and fail for every recognition - the same
    employee would get two identities the moment an operator filled it in, and
    per-employee presence would silently stop resolving. The record id is the
    only identifier both paths actually carry.

    The name is the last resort, for a record with no id. It is a weak key: a
    rename reads as a different person. The `name:` prefix keeps the two
    namespaces apart so nobody is ever counted under both forms.
    """
    record_id = (record_id or "").strip()
    if record_id:
        return f"pid:{record_id}"
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

    Identity comes from the camera record id, falling back to the library
    name (prefixed, see `employee_key`). Two cases cannot be resolved and
    are reported instead of guessed:

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
        # `pid` is the FDSearch record id, the same value the alert stream
        # reports as `human_id`. Keying on it is what makes the two paths agree.
        key = employee_key(pid, name)

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
