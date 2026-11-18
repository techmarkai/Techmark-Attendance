"""Event classification. Pure logic, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import (
    LIBRARY_NAME_CUSTOMERS,
    LIBRARY_NAME_EMPLOYEE,
    ROLE_CUSTOMER,
    ROLE_EMPLOYEE,
    ROLE_OTHER,
)
from .models import FaceMatch


@dataclass(frozen=True, slots=True)
class LibraryMap:
    """Which face library (FDID) means what.

    Discovered from the camera's library names at setup, overridable in
    options. An unknown FDID is deliberately NOT guessed: it becomes
    ROLE_OTHER and is excluded from both employee and visitor figures.
    """

    employee_fdid: str | None = None
    customer_fdid: str | None = None

    @classmethod
    def from_libraries(cls, libraries: list[dict[str, Any]]) -> LibraryMap:
        employee = customer = None
        for lib in libraries:
            name = str(lib.get("name") or "").strip().casefold()
            fdid = str(lib.get("fdid") or lib.get("FDID") or "").strip()
            if not fdid:
                continue
            if name == LIBRARY_NAME_EMPLOYEE.casefold():
                employee = fdid
            elif name == LIBRARY_NAME_CUSTOMERS.casefold():
                customer = fdid
        return cls(employee_fdid=employee, customer_fdid=customer)

    def role_for(self, match: FaceMatch) -> str:
        if self.employee_fdid and match.fdid == self.employee_fdid:
            return ROLE_EMPLOYEE
        if self.customer_fdid and match.fdid == self.customer_fdid:
            return ROLE_CUSTOMER
        # Fall back to the library name the camera reported with the event.
        name = match.library.strip().casefold()
        if name == LIBRARY_NAME_EMPLOYEE.casefold():
            return ROLE_EMPLOYEE
        if name == LIBRARY_NAME_CUSTOMERS.casefold():
            return ROLE_CUSTOMER
        return ROLE_OTHER


def passes_quality(quality: int | None, minimum: int) -> bool:
    """Quality gate for UNMATCHED faces only.

    Matches are gated by the camera's own library threshold on similarity, not
    by the quality score: both matches observed on this camera scored 49-55
    against a library threshold of 80 and were correct. Applying a quality
    floor to matches would discard genuine identifications.
    """
    if quality is None:
        return True
    return quality >= minimum
