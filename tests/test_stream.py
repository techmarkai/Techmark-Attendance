"""Parser and decoder tests, using payload shapes captured from the camera."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from custom_components.techmark_attendance.models import (
    CounterSample,
    CounterWindow,
    FaceMatch,
    FaceUnmatched,
    Heartbeat,
)
from custom_components.techmark_attendance.processor import employee_key
from custom_components.techmark_attendance.stream import (
    MultipartParser,
    UnsafeXMLError,
    safe_fromstring,
    to_event,
)

NOW = datetime(2026, 11, 18, 17, 42, 16)

COUNTING_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<dateTime>2026-11-18T17:42:18+03:00</dateTime>
<eventType>PeopleCounting</eventType>
<channelName>Camera 01</channelName>
<peopleCounting>
<statisticalMethods>realTime</statisticalMethods>
<enter>72</enter>
<exit>67</exit>
<duplicatePeople>58</duplicatePeople>
<countingSceneMode>entrance</countingSceneMode>
</peopleCounting>
</EventNotificationAlert>"""

WINDOW_XML = COUNTING_XML.replace("realTime", "timeRange")

VIDEOLOSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<dateTime>2026-11-18T17:42:18+03:00</dateTime><eventType>videoloss</eventType>
</EventNotificationAlert>"""

VMD_XML = VIDEOLOSS_XML.replace("videoloss", "VMD")


def _match_payload(
    name="Nedal",
    human_id="7",
    fdid="2",
    library="Customers",
    custom_human_id="",
):
    """A face comparison result shaped like the live camera's.

    `human_id` is the matched library record (FDSearch's PID) and
    `customHumanID` is the operator-set id, which is EMPTY on this camera.
    An earlier version of this fixture set both to the same value, which is
    exactly why it never caught the identity bug: the two paths only
    disagree when the fields differ, as they do in reality.
    """
    return {
        "dateTime": "2026-11-18T17:42:16+03:00",
        "eventType": "alarmResult",
        "alarmResult": [
            {
                "errorCode": 1,
                "errorMsg": "ok",
                "targetAttrs": {"pId": "2026111817421860200TPD5usNzdzpCC"},
                "faces": [
                    {
                        "score": {"value": 58},
                        "stayDuration": 2000,
                        "pId": "2026111817421860200l5wl3R7gVwv24",
                        "identify": [
                            {
                                "maxsimilarity": 0.95,
                                "candidate": [
                                    {
                                        "blacklist_id": fdid,
                                        "human_id": human_id,
                                        "customHumanID": custom_human_id,
                                        "similarity": 0.95,
                                        "FDLibName": library,
                                        "FDLibThreshold": 80,
                                        "reserve_field": {"name": name},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


UNMATCHED_PAYLOAD = {
    "dateTime": "2026-11-18T17:42:15+03:00",
    "eventType": "alarmResult",
    "alarmResult": [
        {
            "errorCode": 1073750078,
            "errorMsg": "contrastFailed",
            "targetAttrs": {"pId": "2026111817421851400SidEHPEiwBkfi"},
            "faces": [{"score": {"value": 44}, "identify": [{"maxsimilarity": 0.19}]}],
        }
    ],
}


def _multipart(parts):
    boundary = b"--MIME_boundary"
    out = bytearray()
    for ctype, body in parts:
        out += boundary + b"\r\n"
        out += b"Content-Type: " + ctype + b"\r\n"
        out += b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        out += body + b"\r\n"
    return bytes(out)


# --- multipart parsing ---


def test_parses_parts_with_content_length():
    raw = _multipart(
        [
            (b"application/xml", COUNTING_XML.encode()),
            (b"application/json", json.dumps(_match_payload()).encode()),
        ]
    )
    parser = MultipartParser()
    parts = parser.feed(raw)
    assert len(parts) == 2
    assert parts[0].content_type == "application/xml"
    assert parts[1].content_type == "application/json"


def test_parts_split_across_chunks_are_reassembled():
    """The stream arrives in arbitrary TCP chunks, not whole parts."""
    raw = _multipart([(b"application/xml", COUNTING_XML.encode())])
    parser = MultipartParser()
    collected = []
    for index in range(0, len(raw), 7):
        collected.extend(parser.feed(raw[index : index + 7]))
    assert len(collected) == 1
    assert b"PeopleCounting" in collected[0].body


def test_binary_part_is_returned_but_decodes_to_no_event():
    raw = _multipart([(b"image/jpeg", b"\xff\xd8\xff\xe0 not really a jpeg")])
    part = MultipartParser().feed(raw)[0]
    assert to_event(part, NOW) is None


def test_oversized_buffer_resyncs_instead_of_growing():
    parser = MultipartParser()
    # No boundary ever appears, so nothing can be parsed.
    assert parser.feed(b"x" * (24 * 1024 * 1024 + 10)) == []
    assert parser.feed(_multipart([(b"application/xml", COUNTING_XML.encode())]))


# --- XML safety ---


def test_dtd_is_refused():
    with pytest.raises(UnsafeXMLError):
        safe_fromstring('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')


def test_billion_laughs_is_refused_before_expansion():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><lolz>&lol2;</lolz>'
    )
    with pytest.raises(UnsafeXMLError):
        safe_fromstring(bomb)


def test_event_with_dtd_is_dropped_not_raised():
    """A hostile part must not kill the stream."""
    raw = _multipart(
        [(b"application/xml", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')]
    )
    part = MultipartParser().feed(raw)[0]
    assert to_event(part, NOW) is None


# --- decoding ---


def test_counting_realtime_becomes_a_cumulative_sample():
    part = MultipartParser().feed(_multipart([(b"application/xml", COUNTING_XML.encode())]))[0]
    event = to_event(part, NOW)
    assert isinstance(event, CounterSample)
    assert (event.enter, event.exit, event.duplicate_people) == (72, 67, 58)


def test_counting_timerange_is_a_window_not_a_sample():
    """Mixing the 15-minute window summary into the cumulative series would
    corrupt every delta."""
    part = MultipartParser().feed(_multipart([(b"application/xml", WINDOW_XML.encode())]))[0]
    assert isinstance(to_event(part, NOW), CounterWindow)


def test_videoloss_is_a_heartbeat_and_vmd_is_ignored():
    heartbeat = MultipartParser().feed(_multipart([(b"application/xml", VIDEOLOSS_XML.encode())]))[0]
    assert isinstance(to_event(heartbeat, NOW), Heartbeat)
    vmd = MultipartParser().feed(_multipart([(b"application/xml", VMD_XML.encode())]))[0]
    assert to_event(vmd, NOW) is None


def test_match_carries_identity_and_library():
    part = MultipartParser().feed(
        _multipart([(b"application/json", json.dumps(_match_payload()).encode())])
    )[0]
    event = to_event(part, NOW)
    assert isinstance(event, FaceMatch)
    assert event.name == "Nedal"
    assert event.record_id == "7"  # the matched library record
    assert event.fdid == "2"
    assert event.library == "Customers"
    assert event.similarity == 0.95
    assert event.quality == 58


def test_unmatched_is_decoded_and_never_becomes_a_match():
    part = MultipartParser().feed(
        _multipart([(b"application/json", json.dumps(UNMATCHED_PAYLOAD).encode())])
    )[0]
    event = to_event(part, NOW)
    assert isinstance(event, FaceUnmatched)
    assert event.quality == 44
    assert event.max_similarity == 0.19


def test_employee_with_empty_human_id_still_produces_a_match():
    """The real Employee record on this camera has an EMPTY customHumanID.

    Keying identity on it alone would drop every employee sighting, so the
    library name is the documented fallback.
    """
    payload = _match_payload(name="Yousef", human_id="", fdid="1", library="Employee")
    part = MultipartParser().feed(
        _multipart([(b"application/json", json.dumps(payload).encode())])
    )[0]
    event = to_event(part, NOW)
    assert isinstance(event, FaceMatch)
    assert event.record_id == ""
    assert event.name == "Yousef"
    # With no record id, the name is the documented last resort.
    assert employee_key(event.record_id, event.name) == "name:Yousef"


def test_identity_namespaces_cannot_collide():
    assert employee_key("9", "Nedal") == "pid:9"  # the record id wins
    assert employee_key("", "Nedal") == "name:Nedal"  # then the name
    assert employee_key("", "") == ""
    # The record id wins, so a renamed person keeps one identity.
    assert employee_key("9", "Nedal") == employee_key("9", "Renamed")
    # The prefixes keep the namespaces apart.
    assert employee_key("9", "") != employee_key("", "9")


def test_match_without_candidate_is_dropped():
    payload = _match_payload()
    payload["alarmResult"][0]["faces"][0]["identify"] = []
    part = MultipartParser().feed(
        _multipart([(b"application/json", json.dumps(payload).encode())])
    )[0]
    assert to_event(part, NOW) is None


def test_malformed_json_is_dropped_not_raised():
    raw = _multipart([(b"application/json", b"{not json")])
    part = MultipartParser().feed(raw)[0]
    assert to_event(part, NOW) is None
