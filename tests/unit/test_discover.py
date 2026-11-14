"""Regression tests for Apple TV 4K Gen 4 discovery compatibility.

:copyright: (c) 2026 Paul Giralt <pgiralt@mac.com>
:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, sentinel

from pyatv.const import DeviceModel
import pytest

import discover


@pytest.mark.asyncio
@pytest.mark.parametrize("has_native_enum", [False, True])
@pytest.mark.parametrize(
    ("model", "raw_model", "expected_count"),
    [
        (DeviceModel.Gen4, None, 1),
        (DeviceModel.Gen4K, None, 1),
        (DeviceModel.AppleTV4KGen2, None, 1),
        (DeviceModel.AppleTV4KGen3, None, 1),
        (DeviceModel.Unknown, "AppleTV18,1", 1),
        (DeviceModel.Unknown, None, 0),
        (DeviceModel.Unknown, "AppleTV18,10", 0),
        (DeviceModel.Unknown, "AppleTV99,1", 0),
        (DeviceModel.HomePod, "AppleTV18,1", 0),
    ],
)
async def test_supported_models(
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel,
    raw_model: str | None,
    expected_count: int,
    *,
    has_native_enum: bool,
) -> None:
    """Keep the exact-identifier fallback safe with and without the future enum."""
    members = {name: value for name, value in DeviceModel.__members__.items() if name != "AppleTV4KGen4"}
    if has_native_enum:
        members["AppleTV4KGen4"] = sentinel.gen4
    monkeypatch.setattr(discover, "DeviceModel", SimpleNamespace(**members))
    device = SimpleNamespace(device_info=SimpleNamespace(model=model, raw_model=raw_model))
    monkeypatch.setattr(discover.pyatv, "scan", AsyncMock(return_value=[device]))

    assert await discover.apple_tvs(asyncio.get_running_loop()) == [device] * expected_count


@pytest.mark.asyncio
async def test_native_gen4_without_raw_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future native model must work even without the raw identifier."""
    members = {name: value for name, value in DeviceModel.__members__.items() if name != "AppleTV4KGen4"}
    monkeypatch.setattr(discover, "DeviceModel", SimpleNamespace(**members, AppleTV4KGen4=sentinel.gen4))
    device = SimpleNamespace(device_info=SimpleNamespace(model=sentinel.gen4, raw_model=None))
    monkeypatch.setattr(discover.pyatv, "scan", AsyncMock(return_value=[device]))

    assert await discover.apple_tvs(asyncio.get_running_loop()) == [device]
