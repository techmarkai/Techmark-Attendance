"""Config flow runtime tests.

The flow is the only place a user's camera credentials are handled, and the
only place a duplicate camera can be rejected. None of it was executed by a
test before this file.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "pytest_homeassistant_custom_component",
    reason="runtime tests need the Home Assistant test harness",
)

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.techmark_attendance.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_END,
    DEFAULT_START,
    DOMAIN,
    OPT_END,
    OPT_START,
    OPT_WORKING_DAYS,
)
from custom_components.techmark_attendance.isapi import AuthError, CameraError

USER_INPUT = {
    CONF_HOST: " camera.invalid ",  # deliberately padded; the flow must strip it
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "not-a-real-password",
}

DEVICE_INFO = {
    "deviceName": "test-cam",
    "model": "iDS-TEST",
    "serialNumber": "SN-TEST-0001",
    "macAddress": "00:11:22:33:44:55",
    "firmwareVersion": "V0.0.0",
}


def _client(device_info=None, error: Exception | None = None):
    client = AsyncMock()
    if error is not None:
        client.device_info.side_effect = error
    else:
        client.device_info.return_value = device_info or dict(DEVICE_INFO)
    return client


async def _run_user_step(hass: HomeAssistant, client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    with patch(
        "custom_components.techmark_attendance.config_flow.HikClient",
        return_value=client,
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], dict(USER_INPUT)
        )


async def test_a_reachable_camera_creates_the_entry(hass: HomeAssistant) -> None:
    result = await _run_user_step(hass, _client())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "iDS-TEST"
    # The host must be stored stripped, or every later comparison is off by
    # whitespace the user cannot see.
    assert result["data"][CONF_HOST] == "camera.invalid"


async def test_the_serial_number_is_the_unique_id(hass: HomeAssistant) -> None:
    """Identity must survive a DHCP address change."""
    result = await _run_user_step(hass, _client())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "SN-TEST-0001"


async def test_rejected_credentials_show_invalid_auth_and_keep_the_form(
    hass: HomeAssistant,
) -> None:
    result = await _run_user_step(hass, _client(error=AuthError("denied")))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_an_unreachable_camera_shows_cannot_connect(
    hass: HomeAssistant,
) -> None:
    result = await _run_user_step(hass, _client(error=CameraError("refused")))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_camera_cannot_be_added_twice(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, data=dict(USER_INPUT), unique_id="SN-TEST-0001"
    ).add_to_hass(hass)
    result = await _run_user_step(hass, _client())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_camera_with_no_serial_falls_back_to_the_host(
    hass: HomeAssistant,
) -> None:
    info = dict(DEVICE_INFO) | {"serialNumber": ""}
    result = await _run_user_step(hass, _client(device_info=info))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "camera.invalid"


async def test_new_entries_get_the_agreed_schedule_defaults(
    hass: HomeAssistant,
) -> None:
    """Sat-Thu working week, 09:00-19:00, as settled with the owner."""
    result = await _run_user_step(hass, _client())
    options = result["options"]
    assert options[OPT_WORKING_DAYS] == [0, 1, 2, 3, 5, 6]  # Friday off
    assert options[OPT_START] == DEFAULT_START == "09:00"
    assert options[OPT_END] == DEFAULT_END == "19:00"


async def test_the_password_is_not_in_the_flow_result_title_or_id(
    hass: HomeAssistant,
) -> None:
    """A credential must never end up in a user-visible label."""
    result = await _run_user_step(hass, _client())
    secret = USER_INPUT[CONF_PASSWORD]
    assert secret not in str(result["title"])
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert secret not in str(entry.unique_id)
    assert secret not in str(entry.title)
