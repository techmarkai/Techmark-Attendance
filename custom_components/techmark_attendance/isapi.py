"""Async ISAPI client. Transport and parsing only, no business logic."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date as Date
from typing import Any

import httpx

from .const import (
    ALERT_STREAM_PATH,
    COUNTING_SEARCH_PATH,
    COUNTING_STATISTIC_TYPE,
    DEVICE_INFO_PATH,
    FDLIB_PATH,
    FDSEARCH_FIRST_POSITION,
    FDSEARCH_PATH,
    TIME_PATH,
)
from .stream import UnsafeXMLError, safe_fromstring

_LOGGER = logging.getLogger(__name__)

NS = "http://www.hikvision.com/ver20/XMLSchema"


class CameraError(Exception):
    """The camera could not be reached or returned something unusable."""


class AuthError(CameraError):
    """Credentials were rejected."""


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element, name: str) -> str | None:
    for child in node.iter():
        if _strip_ns(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


def _int(node: ET.Element, name: str, default: int = 0) -> int:
    value = _text(node, name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


class HikClient:
    """Digest-authenticated ISAPI client."""

    def __init__(self, client: httpx.AsyncClient, host: str, username: str, password: str) -> None:
        self._client = client
        self._host = host.strip().rstrip("/")
        self._auth = httpx.DigestAuth(username, password)

    def _url(self, path: str) -> str:
        host = self._host
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return f"{host}{path}"

    async def _request(
        self, method: str, path: str, body: bytes | None = None, timeout: float = 20.0
    ) -> bytes:
        try:
            response = await self._client.request(
                method,
                self._url(path),
                content=body,
                auth=self._auth,
                timeout=timeout,
                headers={"Content-Type": "application/xml"} if body else None,
            )
        except httpx.HTTPError as err:
            raise CameraError(f"request to {path} failed: {err}") from err
        if response.status_code in (401, 403):
            raise AuthError(f"camera rejected credentials for {path}")
        if response.status_code >= 400:
            raise CameraError(f"{path} returned HTTP {response.status_code}")
        return response.content

    async def _xml(self, method: str, path: str, body: bytes | None = None) -> ET.Element:
        raw = await self._request(method, path, body)
        try:
            return safe_fromstring(raw.decode("utf-8", "replace"))
        except (ET.ParseError, UnsafeXMLError, RecursionError) as err:
            raise CameraError(f"unusable XML from {path}: {err}") from err

    # --- identity ---

    async def device_info(self) -> dict[str, str]:
        root = await self._xml("GET", DEVICE_INFO_PATH)
        return {
            key: _text(root, key) or ""
            for key in ("deviceName", "model", "serialNumber", "macAddress", "firmwareVersion")
        }

    async def camera_time(self) -> str | None:
        try:
            root = await self._xml("GET", TIME_PATH)
        except CameraError:
            return None
        return _text(root, "localTime")

    # --- face libraries ---

    async def libraries(self) -> list[dict[str, Any]]:
        root = await self._xml("GET", FDLIB_PATH)
        libs: list[dict[str, Any]] = []
        for node in root.iter():
            if _strip_ns(node.tag) != "FDLibBaseCfg":
                continue
            libs.append(
                {
                    "fdid": _text(node, "FDID") or "",
                    "name": _text(node, "name") or "",
                    "threshold": _int(node, "thresholdValue"),
                }
            )
        return libs

    async def library_records(
        self, fdid: str, max_records: int = 3000
    ) -> tuple[list[dict[str, str]], bool]:
        """Every record in one library, plus whether the read was complete.

        `searchResultPosition` MUST start at 1: the camera answers HTTP 400
        when it is 0.

        Completeness must be *proven*, not assumed. It is True only when the
        camera reported a `totalMatches` and we hold at least that many
        records. A missing or unparsable total means we cannot tell, so the
        read is reported incomplete.

        This fails closed on purpose: a caller treats a complete read as
        authoritative and deactivates every employee missing from it, so
        guessing "complete" here would deactivate real staff the moment a
        firmware stopped reporting the total.
        """
        out: list[dict[str, str]] = []
        total: int | None = None
        position = FDSEARCH_FIRST_POSITION
        page_size = 100
        while len(out) < max_records:
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<FDSearchDescription version="2.0" xmlns="{NS}">'
                "<searchID>TMATT</searchID>"
                f"<searchResultPosition>{position}</searchResultPosition>"
                f"<maxResults>{page_size}</maxResults>"
                f"<FDID>{int(fdid)}</FDID>"
                "</FDSearchDescription>"
            ).encode()
            root = await self._xml("POST", FDSEARCH_PATH, body)
            if total is None:
                reported = _text(root, "totalMatches")
                total = int(reported) if reported and reported.isdigit() else None
            page = [
                {
                    "pid": _text(node, "PID") or "",
                    "name": _text(node, "name") or "",
                    "human_id": _text(node, "customHumanID") or "",
                }
                for node in root.iter()
                # Casing of this container differs between ISAPI responses on
                # this firmware, so match it case-insensitively.
                if _strip_ns(node.tag).casefold() == "matchelement"
            ]
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            position += len(page)

        records = out[:max_records]
        # Fail closed: no reported total means completeness is unprovable.
        # The `len(records) >= total` test also covers the max_records cap,
        # since a library larger than the cap cannot satisfy it.
        complete = total is not None and len(records) >= total
        return records, complete

    # --- people counting ---

    async def counting_statistics(self, day: Date) -> list[dict[str, Any]]:
        """Hourly buckets for one day, with demographics.

        `statisticType` must be exactly `duplicatePeople`: the camera rejects
        `enter`, `leave`, and comma-separated lists with
        "Unknow tag <statisticType>". That single value returns every counter
        plus the gender, age and mask breakdowns anyway.
        """
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<CountingStatisticsDescription version="2.0" xmlns="{NS}">'
            "<searchID>TMATTC</searchID><timeSpanList><timeSpan>"
            f"<startTime>{day.isoformat()}T00:00:00</startTime>"
            f"<endTime>{day.isoformat()}T23:59:59</endTime>"
            "</timeSpan></timeSpanList><reportType>daily</reportType>"
            f"<statisticType>{COUNTING_STATISTIC_TYPE}</statisticType>"
            "</CountingStatisticsDescription>"
        ).encode()
        root = await self._xml("POST", COUNTING_SEARCH_PATH, body)
        return [
            self._parse_bucket(node)
            for node in root.iter()
            if _strip_ns(node.tag) == "matchElement"
        ]

    @staticmethod
    def _parse_bucket(node: ET.Element) -> dict[str, Any]:
        start = _text(node, "startTime") or ""
        hour = 0
        if len(start) >= 13 and start[11:13].isdigit():
            hour = int(start[11:13])
        bucket: dict[str, Any] = {
            "hour": hour,
            "start": start,
            "enter": _int(node, "enterCount"),
            "exit": _int(node, "exitCount"),
            "dedup": _int(node, "duplicatePeopleCount"),
        }
        for list_tag, item_tag, key in (
            ("GenderList", "Gender", "gender"),
            ("AgeGroupList", "AgeGroup", "age"),
            ("MaskList", "Mask", "mask"),
        ):
            values: dict[str, int] = {}
            for container in node.iter():
                if _strip_ns(container.tag) != list_tag:
                    continue
                for item in container:
                    if _strip_ns(item.tag) != item_tag:
                        continue
                    value = _text(item, "value")
                    if value:
                        values[value] = _int(item, "count")
            bucket[key] = values
        return bucket

    # --- alert stream ---

    @asynccontextmanager
    async def alert_stream(self) -> AsyncIterator[AsyncIterator[bytes]]:
        """Long-lived multipart event stream."""
        try:
            async with self._client.stream(
                "GET",
                self._url(ALERT_STREAM_PATH),
                auth=self._auth,
                timeout=httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0),
            ) as response:
                if response.status_code in (401, 403):
                    raise AuthError("camera rejected credentials for the alert stream")
                if response.status_code >= 400:
                    raise CameraError(f"alert stream returned HTTP {response.status_code}")
                yield response.aiter_bytes()
        except httpx.HTTPError as err:
            raise CameraError(f"alert stream failed: {err}") from err
