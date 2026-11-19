"""ISAPI client tests, driven by a fake transport.

The library read decides whether a roster refresh is treated as
authoritative, and an authoritative refresh deactivates every employee
missing from it. So "is this read complete?" has to fail closed.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from custom_components.techmark_attendance.isapi import AuthError, CameraError, HikClient

NS = "http://www.hikvision.com/ver20/XMLSchema"


def fdsearch_response(records: list[tuple[str, str, str]], total: str | None) -> str:
    """An FDSearch body shaped like the camera's, optionally without a total."""
    total_tag = f"<totalMatches>{total}</totalMatches>" if total is not None else ""
    elements = "".join(
        f"<MatchElement><PID>{pid}</PID><name>{name}</name>"
        f"<customHumanID>{human_id}</customHumanID></MatchElement>"
        for pid, name, human_id in records
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<FDSearchResult version="2.0" xmlns="{NS}">'
        f"<responseStatusStrg>OK</responseStatusStrg>"
        f"<numOfMatches>{len(records)}</numOfMatches>{total_tag}"
        f"<MatchList>{elements}</MatchList></FDSearchResult>"
    )


def client_for(handler) -> HikClient:
    transport = httpx.MockTransport(handler)
    return HikClient(httpx.AsyncClient(transport=transport), "camera.test", "u", "p")


# --- the fail-closed guarantee -----------------------------------------


@pytest.mark.asyncio
async def test_read_with_a_matching_total_is_complete():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fdsearch_response([("1", "Yousef", "")], total="1"))

    records, complete = await client_for(handler).library_records("1")
    assert len(records) == 1
    assert complete is True


@pytest.mark.asyncio
async def test_missing_total_is_reported_incomplete_not_assumed_complete():
    """The flagged fail-open: without a total, completeness is unprovable.

    Assuming "complete" here would let an authoritative roster refresh
    deactivate every employee the camera happened not to return.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fdsearch_response([("1", "Yousef", "")], total=None))

    records, complete = await client_for(handler).library_records("1")
    assert len(records) == 1
    assert complete is False


@pytest.mark.asyncio
async def test_unparsable_total_is_also_incomplete():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fdsearch_response([("1", "Yousef", "")], total="many"))

    _, complete = await client_for(handler).library_records("1")
    assert complete is False


@pytest.mark.asyncio
async def test_a_library_larger_than_the_cap_is_incomplete():
    """A truncated read must never pass as the whole library."""
    def handler(request: httpx.Request) -> httpx.Response:
        page = [(str(i), f"E{i}", "") for i in range(100)]
        return httpx.Response(200, text=fdsearch_response(page, total="5000"))

    records, complete = await client_for(handler).library_records("1", max_records=200)
    assert len(records) == 200
    assert complete is False


@pytest.mark.asyncio
async def test_fewer_records_than_the_total_is_incomplete():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fdsearch_response([("1", "Yousef", "")], total="9"))

    _, complete = await client_for(handler).library_records("1")
    assert complete is False


# --- parsing and paging -------------------------------------------------


@pytest.mark.asyncio
async def test_records_are_parsed_with_the_expected_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=fdsearch_response([("3", "Nedal", "TMV-10")], total="1")
        )

    records, _ = await client_for(handler).library_records("1")
    assert records[0] == {"pid": "3", "name": "Nedal", "human_id": "TMV-10"}


@pytest.mark.asyncio
async def test_paging_starts_at_position_one():
    """Position 0 makes the camera answer HTTP 400."""
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, text=fdsearch_response([("1", "Yousef", "")], total="1"))

    await client_for(handler).library_records("1")
    assert b"<searchResultPosition>1</searchResultPosition>" in seen[0]


@pytest.mark.asyncio
async def test_empty_library_with_a_zero_total_is_complete():
    """An authoritative empty read is how "the library was emptied" is known."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=fdsearch_response([], total="0"))

    records, complete = await client_for(handler).library_records("1")
    assert records == []
    assert complete is True


# --- errors -------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_credentials_raise_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="denied")

    with pytest.raises(AuthError):
        await client_for(handler).device_info()


@pytest.mark.asyncio
async def test_server_error_raises_camera_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(CameraError):
        await client_for(handler).device_info()


@pytest.mark.asyncio
async def test_xml_with_a_dtd_is_refused_even_from_the_camera():
    """The camera is a trust boundary, so a DTD is refused on every read."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><DeviceInfo/>',
        )

    with pytest.raises(CameraError):
        await client_for(handler).device_info()


@pytest.mark.asyncio
async def test_camera_time_returns_none_rather_than_raising():
    """A failed clock read must read as unavailable, never as a skew of zero."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert await client_for(handler).camera_time() is None


@pytest.mark.asyncio
async def test_only_read_only_methods_reach_the_camera():
    """Every request this client makes must be a GET or a search POST."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("FDSearch"):
            return httpx.Response(200, text=fdsearch_response([], total="0"))
        return httpx.Response(
            200,
            text=f'<?xml version="1.0"?><DeviceInfo xmlns="{NS}"><model>X</model></DeviceInfo>',
        )

    client = client_for(handler)
    await client.device_info()
    await client.libraries()
    await client.library_records("1")

    assert seen, "no requests were made"
    for method, path in seen:
        assert method in ("GET", "POST")
        if method == "POST":
            assert path.endswith(("FDSearch", "search")), f"non-search POST to {path}"


@pytest.mark.asyncio
async def test_counting_buckets_include_the_glasses_breakdown():
    """The camera sends GlassList and it was being dropped.

    Every other attribute list was parsed, so the report's Glass section could
    never populate and the glass consistency check in evaluate_bucket never
    ran against real data.
    """
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<CountingStatisticsResult version="2.0" xmlns="{NS}">'
        "<matchElement>"
        "<startTime>2026-11-19T09:00:00</startTime>"
        "<enterCount>19</enterCount><exitCount>16</exitCount>"
        "<duplicatePeopleCount>14</duplicatePeopleCount>"
        "<GenderList><Gender><value>male</value><count>5</count></Gender></GenderList>"
        "<MaskList><Mask><value>no</value><count>5</count></Mask></MaskList>"
        "<GlassList>"
        "<Glass><value>yes</value><count>1</count></Glass>"
        "<Glass><value>sunglasses</value><count>0</count></Glass>"
        "<Glass><value>no</value><count>4</count></Glass>"
        "</GlassList>"
        "</matchElement></CountingStatisticsResult>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    buckets = await client_for(handler).counting_statistics(date(2026, 11, 19))
    assert buckets[0]["glass"] == {"yes": 1, "sunglasses": 0, "no": 4}
    # The other categories must keep working.
    assert buckets[0]["gender"] == {"male": 5}
    assert buckets[0]["enter"] == 19
