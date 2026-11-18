"""Incremental multipart parser and event decoding for the camera alert stream.

Pure functions only. No network, no Home Assistant imports, so this is fully
unit-testable against captured bytes.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from .models import (
    CounterSample,
    CounterWindow,
    Event,
    FaceMatch,
    FaceUnmatched,
    Heartbeat,
    Part,
    RawEvent,
)

_LOGGER = logging.getLogger(__name__)

# A single part larger than this is treated as corrupt rather than buffered
# forever; the camera's scene JPEGs are well under it.
MAX_PART_BYTES = 12 * 1024 * 1024
MAX_BUFFER_BYTES = 24 * 1024 * 1024


class MultipartParser:
    """Feed bytes in, get complete parts out.

    The boundary is discovered from the first delimiter line, because the
    camera's Content-Type header is not always available to us.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._boundary: bytes | None = None

    def feed(self, chunk: bytes) -> list[Part]:
        self._buf += chunk
        if len(self._buf) > MAX_BUFFER_BYTES:
            # Never grow without bound on a malformed stream.
            _LOGGER.warning("alert stream buffer exceeded %d bytes, resyncing", MAX_BUFFER_BYTES)
            self._buf.clear()
            self._boundary = None
            return []
        parts: list[Part] = []
        while (part := self._next_part()) is not None:
            parts.append(part)
        return parts

    def _find_boundary(self) -> bool:
        if self._boundary is not None:
            return True
        start = self._buf.find(b"--")
        if start == -1:
            return False
        end = self._buf.find(b"\r\n", start)
        if end == -1:
            end = self._buf.find(b"\n", start)
            if end == -1:
                return False
        line = bytes(self._buf[start:end]).rstrip()
        if len(line) <= 2:
            return False
        self._boundary = line
        return True

    def _next_part(self) -> Part | None:
        if not self._find_boundary():
            return None
        assert self._boundary is not None

        pos = self._buf.find(self._boundary)
        if pos == -1:
            return None
        after = pos + len(self._boundary)

        # Skip the CRLF (or LF) that ends the delimiter line.
        if self._buf[after : after + 2] == b"\r\n":
            after += 2
        elif self._buf[after : after + 1] == b"\n":
            after += 1
        elif self._buf[after : after + 2] == b"--":
            self._buf.clear()
            return None
        else:
            return None

        head_end = self._buf.find(b"\r\n\r\n", after)
        sep = 4
        if head_end == -1:
            head_end = self._buf.find(b"\n\n", after)
            sep = 2
            if head_end == -1:
                return None

        headers = _parse_headers(bytes(self._buf[after:head_end]))
        body_start = head_end + sep

        length = headers.get("content-length")
        if length is not None and length.strip().isdigit():
            size = int(length)
            if size > MAX_PART_BYTES:
                _LOGGER.warning("discarding oversized part: %d bytes", size)
                del self._buf[:body_start]
                self._boundary = None
                return None
            if len(self._buf) < body_start + size:
                return None
            body = bytes(self._buf[body_start : body_start + size])
            del self._buf[: body_start + size]
            return Part(headers, body)

        # No Content-Length: the body runs to the next delimiter.
        nxt = self._buf.find(self._boundary, body_start)
        if nxt == -1:
            return None
        body = bytes(self._buf[body_start:nxt]).rstrip(b"\r\n")
        del self._buf[:nxt]
        return Part(headers, body)


def _parse_headers(blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in blob.split(b"\n"):
        line = raw.strip()
        if not line or b":" not in line:
            continue
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def _camera_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def identity_key(human_id: str, name: str) -> str:
    """Stable identity for a matched person.

    `customHumanID` is preferred, but the Employee record on this camera has
    an empty one, so the library name is used as a fallback rather than
    discarding the match. The `name:` prefix keeps the two namespaces apart so
    a person cannot be counted twice under both forms.
    """
    human_id = human_id.strip()
    if human_id:
        return human_id
    name = name.strip()
    return f"name:{name}" if name else ""


class UnsafeXMLError(ValueError):
    """Raised when a document declares a DTD or entities."""


def safe_fromstring(text: str) -> ET.Element:
    """Parse XML, refusing any document type declaration.

    Both XXE and entity-expansion ("billion laughs") attacks require a DTD or
    an ENTITY declaration, so refusing those outright defeats them without
    pulling in another dependency. The camera never sends one.
    """
    head = text[:4096].lower()
    if "<!doctype" in head or "<!entity" in head:
        raise UnsafeXMLError("XML declares a DTD or entities")
    return ET.fromstring(text)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(root: ET.Element, name: str) -> str | None:
    for node in root.iter():
        if _strip_ns(node.tag) == name and node.text is not None:
            return node.text.strip()
    return None


def _int_or_none(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def to_event(part: Part, received: datetime) -> Event | None:
    """Decode one part into an event, or None if it carries no event."""
    ctype = part.content_type
    if ctype.startswith("image/"):
        return None
    if ctype == "application/json":
        return _from_json(part.body, received)
    if ctype in ("application/xml", "text/xml") or part.body.lstrip().startswith(b"<"):
        return _from_xml(part.body, received)
    return None


def _from_xml(body: bytes, received: datetime) -> Event | None:
    try:
        root = safe_fromstring(body.decode("utf-8", "replace"))
    except (ET.ParseError, UnsafeXMLError, RecursionError) as err:
        _LOGGER.debug("rejected XML part (%d bytes): %s", len(body), err)
        return None

    event_type = _xml_text(root, "eventType") or ""
    camera_time = _camera_time(_xml_text(root, "dateTime"))

    if event_type == "videoloss":
        return Heartbeat(received=received)

    if event_type == "PeopleCounting":
        enter = _int_or_none(_xml_text(root, "enter"))
        exit_ = _int_or_none(_xml_text(root, "exit"))
        dedup = _int_or_none(_xml_text(root, "duplicatePeople"))
        if enter is None or exit_ is None or dedup is None:
            return None
        method = (_xml_text(root, "statisticalMethods") or "").strip()
        if method == "timeRange":
            return CounterWindow(received, camera_time, enter, exit_, dedup)
        return CounterSample(received, camera_time, enter, exit_, dedup)

    if event_type in ("", "VMD"):
        # VMD is pure noise on this camera: 209 events in a 4-minute sample.
        return None
    return RawEvent(received=received, event_type=event_type)


def _from_json(body: bytes, received: datetime) -> Event | None:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        _LOGGER.debug("unparsable JSON part (%d bytes)", len(body))
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("eventType") != "alarmResult":
        etype = payload.get("eventType")
        return RawEvent(received, str(etype)) if etype else None

    results = payload.get("alarmResult")
    if not isinstance(results, list) or not results:
        return None
    result = results[0]
    if not isinstance(result, dict):
        return None

    camera_time = _camera_time(payload.get("dateTime"))
    faces = result.get("faces")
    face: dict[str, Any] = faces[0] if isinstance(faces, list) and faces and isinstance(faces[0], dict) else {}
    quality = _dig_int(face, "score", "value")
    stay_ms = face.get("stayDuration") if isinstance(face.get("stayDuration"), int) else None
    pid = str(result.get("targetAttrs", {}).get("pId") or face.get("pId") or "")

    if result.get("errorMsg") != "ok":
        return FaceUnmatched(
            received=received,
            camera_time=camera_time,
            quality=quality,
            max_similarity=_first_max_similarity(face),
            pid=pid,
        )

    candidate = _first_candidate(face)
    if candidate is None:
        return None
    reserve = candidate.get("reserve_field") or {}
    name = str(reserve.get("name") or "").strip()
    # The Employee library record on this camera has an EMPTY customHumanID,
    # so an identity keyed only on human_id would drop every employee match.
    # Fall back to the library name, which is unique within a library.
    human_id = identity_key(
        str(candidate.get("human_id") or candidate.get("customHumanID") or ""), name
    )
    if not human_id:
        return None
    similarity = candidate.get("similarity")
    return FaceMatch(
        received=received,
        camera_time=camera_time,
        fdid=str(candidate.get("blacklist_id") or ""),
        library=str(candidate.get("FDLibName") or ""),
        library_threshold=_as_int(candidate.get("FDLibThreshold")),
        human_id=human_id,
        name=name or human_id,
        similarity=float(similarity) if isinstance(similarity, (int, float)) else 0.0,
        quality=quality,
        stay_ms=stay_ms,
        pid=pid,
    )


def _first_candidate(face: dict[str, Any]) -> dict[str, Any] | None:
    identify = face.get("identify")
    if not isinstance(identify, list):
        return None
    for entry in identify:
        if not isinstance(entry, dict):
            continue
        candidates = entry.get("candidate")
        if isinstance(candidates, list):
            for cand in candidates:
                if isinstance(cand, dict):
                    return cand
    return None


def _first_max_similarity(face: dict[str, Any]) -> float | None:
    identify = face.get("identify")
    if not isinstance(identify, list):
        return None
    for entry in identify:
        if isinstance(entry, dict) and isinstance(entry.get("maxsimilarity"), (int, float)):
            return float(entry["maxsimilarity"])
    return None


def _dig_int(obj: dict[str, Any], *path: str) -> int | None:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return _as_int(cur)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
