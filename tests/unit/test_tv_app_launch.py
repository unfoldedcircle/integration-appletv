"""Regression tests for Apple TV app discovery and launching.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyatv
from pyatv.const import FeatureName, Protocol
import pytest
from ucapi import StatusCodes
from ucapi.media_player import Attributes as MediaAttr

from config import AtvDevice
from tv import AppleTv, EVENTS, PARTIAL_DISCOVERY_RETRIES

pytestmark = pytest.mark.asyncio


def _device() -> AtvDevice:
    return AtvDevice(
        identifier="test-device",
        name="Test Apple TV",
        credentials=[
            {"protocol": "airplay", "credentials": "airplay-creds"},
            {"protocol": "companion", "credentials": "companion-creds"},
        ],
    )


async def test_partial_discovery_is_retried_instead_of_connected() -> None:
    """An AirPlay-only result must not end the connection retry loop."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    conf = MagicMock()
    conf.name = "Test Apple TV"
    conf.get_service.side_effect = lambda protocol: object() if protocol is Protocol.AirPlay else None
    apple_tv._apple_tv_conf = conf  # noqa: SLF001

    with patch("tv.pyatv.connect", new_callable=AsyncMock) as connect:
        await apple_tv._connect(conf)  # noqa: SLF001

    connect.assert_not_awaited()
    assert apple_tv._apple_tv_conf is None  # noqa: SLF001


async def test_stale_app_list_is_cleared_when_companion_is_unavailable() -> None:
    """A failed refresh must not leave unavailable applications selectable."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.apps.app_list = AsyncMock(side_effect=pyatv.exceptions.NotSupportedError())  # noqa: SLF001

    await apple_tv._update_app_list()  # noqa: SLF001

    assert apple_tv.app_names == []


async def test_launch_app_returns_service_unavailable_when_feature_is_missing() -> None:
    """App launching must fail cleanly on a partial connection."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.features.in_state.return_value = False  # noqa: SLF001

    result = await apple_tv.launch_app("YouTube")

    assert result == StatusCodes.SERVICE_UNAVAILABLE
    apple_tv._atv.apps.launch_app.assert_not_called()  # noqa: SLF001
    apple_tv._atv.features.in_state.assert_called_once_with(  # noqa: SLF001
        pyatv.const.FeatureState.Available,
        FeatureName.LaunchApp,
    )


async def test_launch_known_app_not_supported_returns_service_unavailable() -> None:
    """NotSupportedError for a cached application must not become a server error."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.features.in_state.return_value = True  # noqa: SLF001
    apple_tv._atv.apps.launch_app = AsyncMock(side_effect=pyatv.exceptions.NotSupportedError())  # noqa: SLF001

    result = await apple_tv.launch_app("YouTube")

    assert result == StatusCodes.SERVICE_UNAVAILABLE
    apple_tv._atv.apps.launch_app.assert_awaited_once_with("com.google.ios.youtube")  # noqa: SLF001


async def test_launch_app_uses_bundle_id() -> None:
    """A cached application name resolves to its bundle identifier."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.features.in_state.return_value = True  # noqa: SLF001
    apple_tv._atv.apps.launch_app = AsyncMock()  # noqa: SLF001

    result = await apple_tv.launch_app("YouTube")

    assert result == StatusCodes.OK
    apple_tv._atv.apps.launch_app.assert_awaited_once_with("com.google.ios.youtube")  # noqa: SLF001


@pytest.mark.parametrize("attempt", range(PARTIAL_DISCOVERY_RETRIES))
async def test_partial_discovery_retries_before_fallback(attempt: int) -> None:
    """Incomplete scans are retried before falling back to AirPlay-only control."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._connection_attempts = attempt  # noqa: SLF001
    conf = MagicMock()
    conf.name = "Test Apple TV"
    conf.get_service.side_effect = lambda protocol: object() if protocol is Protocol.AirPlay else None
    apple_tv._apple_tv_conf = conf  # noqa: SLF001

    with patch("tv.pyatv.connect", new_callable=AsyncMock) as connect:
        await apple_tv._connect(conf)  # noqa: SLF001

    connect.assert_not_awaited()
    assert apple_tv._apple_tv_conf is None  # noqa: SLF001


async def test_partial_discovery_falls_back_to_airplay() -> None:
    """AirPlay-only connections remain usable after the discovery retry budget."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._connection_attempts = PARTIAL_DISCOVERY_RETRIES  # noqa: SLF001
    conf = MagicMock()
    conf.name = "Test Apple TV"
    conf.get_service.side_effect = lambda protocol: object() if protocol is Protocol.AirPlay else None

    with patch("tv.pyatv.connect", new_callable=AsyncMock) as connect:
        await apple_tv._connect(conf)  # noqa: SLF001

    connect.assert_awaited_once_with(conf, asyncio.get_running_loop())
    assert apple_tv._atv is connect.return_value  # noqa: SLF001


async def test_missing_all_protocols_does_not_trigger_partial_connection() -> None:
    """Fallback cannot connect if none of the configured protocols is available."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._connection_attempts = PARTIAL_DISCOVERY_RETRIES  # noqa: SLF001
    conf = MagicMock()
    conf.name = "Test Apple TV"
    conf.get_service.return_value = None
    apple_tv._apple_tv_conf = conf  # noqa: SLF001

    with patch("tv.pyatv.connect", new_callable=AsyncMock) as connect:
        await apple_tv._connect(conf)  # noqa: SLF001

    connect.assert_not_awaited()
    assert apple_tv._apple_tv_conf is None  # noqa: SLF001


async def test_empty_app_refresh_preserves_cached_applications() -> None:
    """An empty response must not desynchronize visible app names and bundle IDs."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.apps.app_list = AsyncMock(return_value=[])  # noqa: SLF001

    with patch.object(apple_tv.events, "emit") as emit:
        await apple_tv._update_app_list()  # noqa: SLF001

    assert apple_tv._app_list == {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    emit.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [pyatv.exceptions.ProtocolError(), pyatv.exceptions.BlockedStateError()],
)
async def test_transient_app_refresh_error_preserves_cache(error: Exception) -> None:
    """Protocol and blocked-state errors must preserve the previous application list."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.apps.app_list = AsyncMock(side_effect=error)  # noqa: SLF001

    with patch.object(apple_tv.events, "emit") as emit:
        await apple_tv._update_app_list()  # noqa: SLF001

    assert apple_tv._app_list == {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    emit.assert_not_called()


async def test_successful_app_refresh_replaces_mapping_and_emits_update() -> None:
    """A successful fetch must replace stale apps with the newly discovered list."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"Old app": "com.example.old"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.apps.app_list = AsyncMock(  # noqa: SLF001
        return_value=[SimpleNamespace(name="YouTube", identifier="com.google.ios.youtube")]
    )

    with patch.object(apple_tv.events, "emit") as emit:
        await apple_tv._update_app_list()  # noqa: SLF001

    assert apple_tv._app_list == {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    emit.assert_called_once_with(EVENTS.UPDATE, apple_tv.identifier, {MediaAttr.SOURCE_LIST: ["YouTube"]})


async def test_app_refresh_keeps_bundle_ids_available_during_fetch() -> None:
    """App launching during the network refresh must still use the cached bundle ID."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
    apple_tv._atv = MagicMock()  # noqa: SLF001
    apple_tv._atv.features.in_state.return_value = True  # noqa: SLF001
    apple_tv._atv.apps.launch_app = AsyncMock()  # noqa: SLF001
    started = asyncio.Event()
    resume = asyncio.Event()

    async def delayed_app_list() -> list[SimpleNamespace]:
        started.set()
        await resume.wait()
        return [SimpleNamespace(name="Netflix", identifier="com.netflix.Netflix")]

    apple_tv._atv.apps.app_list = AsyncMock(side_effect=delayed_app_list)  # noqa: SLF001
    task = asyncio.create_task(apple_tv._update_app_list())  # noqa: SLF001
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert apple_tv._app_list == {"YouTube": "com.google.ios.youtube"}  # noqa: SLF001
        assert await apple_tv.launch_app("YouTube") == StatusCodes.OK
        apple_tv._atv.apps.launch_app.assert_awaited_once_with("com.google.ios.youtube")  # noqa: SLF001
    finally:
        resume.set()
        await task

    assert apple_tv._app_list == {"Netflix": "com.netflix.Netflix"}  # noqa: SLF001
