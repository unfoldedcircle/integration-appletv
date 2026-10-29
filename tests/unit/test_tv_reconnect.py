"""
Tests for fast reconnect after wake-up: network retry, command wait, cache and refresh skipping.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

# ruff: noqa: SLF001  # tests access private members
# pyright: reportPrivateUsage=false

import asyncio
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

import pyatv
import pytest
from ucapi import StatusCodes
from ucapi.media_player import Attributes as MediaAttr

from config import AtvDevice
import monkey_patch
import tv
from tv import AppleTv, ConnectResult, ConnectStalledError

pytestmark = pytest.mark.asyncio


def _device() -> AtvDevice:
    return AtvDevice(
        identifier="DE:73:7A:0E:DD:5A",
        name="Test Apple TV",
        credentials=[
            {"protocol": "airplay", "credentials": "airplay-creds"},
            {"protocol": "companion", "credentials": "companion-creds"},
        ],
    )


def _network_error() -> Exception:
    err = pyatv.exceptions.ProtocolError("Failed to set up remote control channel")
    err.__cause__ = OSError(101, "Network is unreachable")
    return err


def _app(name: str, identifier: str) -> MagicMock:
    app = MagicMock()
    app.name = name
    app.identifier = identifier
    return app


# ---- network retry ----


async def test_connect_once_reports_network_unreachable() -> None:
    """A network error is reported as NETWORK_UNREACHABLE and keeps the cached configuration."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    conf = MagicMock()
    apple_tv._apple_tv_conf = conf
    apple_tv._connect = AsyncMock(side_effect=_network_error())

    assert await apple_tv._connect_once() is ConnectResult.NETWORK_UNREACHABLE
    assert apple_tv._apple_tv_conf is conf


async def test_connect_once_reports_direct_network_error() -> None:
    """A network error raised directly, not as cause, is also reported as NETWORK_UNREACHABLE."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    conf = MagicMock()
    apple_tv._apple_tv_conf = conf
    apple_tv._connect = AsyncMock(side_effect=OSError(101, "Network is unreachable"))

    assert await apple_tv._connect_once() is ConnectResult.NETWORK_UNREACHABLE
    assert apple_tv._apple_tv_conf is conf


async def test_connect_once_other_error_resets_conf() -> None:
    """Other errors are reported as FAILED and drop the cached configuration."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._apple_tv_conf = MagicMock()
    apple_tv._connect = AsyncMock(side_effect=pyatv.exceptions.ConnectionFailedError("refused"))

    assert await apple_tv._connect_once() is ConnectResult.FAILED
    assert apple_tv._apple_tv_conf is None


async def test_network_unreachable_is_retried_without_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry fast while the network is unreachable, without using the backoff."""
    monkeypatch.setattr(tv, "NETWORK_RETRY_INTERVAL", 0)
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    results = [ConnectResult.NETWORK_UNREACHABLE, ConnectResult.NETWORK_UNREACHABLE, ConnectResult.CONNECTED]

    calls = await _run_connect_loop(apple_tv, results)

    assert calls == 3
    apple_tv._backoff.assert_not_called()


async def test_network_unreachable_uses_backoff_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the normal backoff after NETWORK_RETRY_TIMEOUT."""
    monkeypatch.setattr(tv, "NETWORK_RETRY_TIMEOUT", 0)
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())

    calls = await _run_connect_loop(apple_tv, [ConnectResult.NETWORK_UNREACHABLE, ConnectResult.CONNECTED])

    assert calls == 2
    apple_tv._backoff.assert_called_once()


async def test_network_retry_time_starts_at_first_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Time spent before the first network error does not reduce the fast retry time."""
    monkeypatch.setattr(tv, "NETWORK_RETRY_INTERVAL", 0)
    monkeypatch.setattr(tv, "NETWORK_RETRY_TIMEOUT", 0.05)
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    results = [
        ConnectResult.FAILED,
        ConnectResult.NETWORK_UNREACHABLE,
        ConnectResult.NETWORK_UNREACHABLE,
        ConnectResult.CONNECTED,
    ]

    calls = await _run_connect_loop(apple_tv, results, backoff=0.1)

    assert calls == 4
    apple_tv._backoff.assert_called_once()


async def test_network_retry_stops_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not retry after disconnect() disabled the device."""
    monkeypatch.setattr(tv, "NETWORK_RETRY_INTERVAL", 0)
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())

    calls = await _run_connect_loop(apple_tv, [ConnectResult.NETWORK_UNREACHABLE], disable_on_call=True)

    assert calls == 1


# ---- stalled connect ----


async def test_stalled_connect_is_retried_without_backoff() -> None:
    """A stalled connect is started again at once."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    stall_timeouts: list[float | None] = []
    results = [ConnectResult.NETWORK_UNREACHABLE, ConnectResult.STALLED, ConnectResult.CONNECTED]

    calls = await _run_connect_loop(apple_tv, results, stall_timeouts=stall_timeouts)

    assert calls == 3
    assert stall_timeouts == [tv.CONNECT_STALL_TIMEOUT, tv.CONNECT_STALL_TIMEOUT, None]
    apple_tv._backoff.assert_not_called()


async def test_stall_restarts_are_limited() -> None:
    """After CONNECT_STALL_RESTARTS stalls, connects use the normal timeout."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    stall_timeouts: list[float | None] = []
    results = [ConnectResult.STALLED] * tv.CONNECT_STALL_RESTARTS + [ConnectResult.CONNECTED]

    calls = await _run_connect_loop(apple_tv, results, stall_timeouts=stall_timeouts)

    assert calls == tv.CONNECT_STALL_RESTARTS + 1
    assert stall_timeouts == [tv.CONNECT_STALL_TIMEOUT] * tv.CONNECT_STALL_RESTARTS + [None]
    apple_tv._backoff.assert_not_called()


async def test_connect_once_reports_stalled_and_keeps_conf() -> None:
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    conf = MagicMock()
    apple_tv._apple_tv_conf = conf
    apple_tv._connect = AsyncMock(side_effect=ConnectStalledError("connect stalled after 0.7s"))

    assert await apple_tv._connect_once(0.7) is ConnectResult.STALLED
    assert apple_tv._apple_tv_conf is conf


async def test_connect_raises_stalled_after_stall_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    started = asyncio.Event()

    async def connect(*_args: object) -> None:
        started.set()
        await asyncio.sleep(100)

    monkeypatch.setattr(tv.pyatv, "connect", connect)
    conf = MagicMock()
    conf.name = _device().name

    with pytest.raises(ConnectStalledError):
        await apple_tv._connect(conf, 0.01)
    assert started.is_set()
    assert apple_tv._atv is None


async def test_connect_progress_extends_stall_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connect that reports progress is not stopped, even if it takes longer than the stall timeout."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    atv = MagicMock()

    async def connect(*_args: object) -> MagicMock:
        for _ in range(5):
            await asyncio.sleep(0.01)
            monkey_patch._report_progress()
        return atv

    monkeypatch.setattr(tv.pyatv, "connect", connect)
    conf = MagicMock()
    conf.name = _device().name

    await apple_tv._connect(conf, 0.03)
    assert apple_tv._atv is atv
    assert monkey_patch.connect_progress.get() is None


async def test_connect_stalls_when_progress_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stall timeout starts again after each progress report."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())

    async def connect(*_args: object) -> None:
        await asyncio.sleep(0.01)
        monkey_patch._report_progress()
        await asyncio.sleep(100)

    monkeypatch.setattr(tv.pyatv, "connect", connect)
    conf = MagicMock()
    conf.name = _device().name

    start = time.monotonic()
    with pytest.raises(ConnectStalledError):
        await apple_tv._connect(conf, 0.03)
    assert time.monotonic() - start >= 0.04
    assert apple_tv._atv is None


# ---- command wait ----


async def test_command_waits_for_connect_task() -> None:
    """A command sent while connecting runs as soon as the connect task is done."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    atv = MagicMock()
    atv.remote_control.down = AsyncMock()

    async def connect_loop() -> None:
        await asyncio.sleep(0.01)
        apple_tv._atv = atv

    async def connect() -> None:
        apple_tv._connect_task = asyncio.create_task(connect_loop())

    apple_tv.connect = connect

    start = time.monotonic()
    assert await apple_tv.cursor_down() == StatusCodes.OK
    assert time.monotonic() - start < tv.CONNECT_WAIT_FOR_COMMAND
    atv.remote_control.down.assert_awaited_once()


async def test_command_without_connect_task_is_unavailable() -> None:
    """Without a running connect task the command fails at once."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv.connect = AsyncMock()

    assert await apple_tv.cursor_down() == StatusCodes.SERVICE_UNAVAILABLE


# ---- cache ----


async def test_app_list_cache_round_trip(tmp_path: Path) -> None:
    """A fetched app list is written to the cache and loaded by a new instance."""
    loop = asyncio.get_running_loop()
    apple_tv = AppleTv(_device(), loop, cache_dir=tmp_path)
    apple_tv._atv = MagicMock()
    apple_tv._atv.apps.app_list = AsyncMock(return_value=[_app("YouTube", "com.google.ios.youtube")])

    await apple_tv._update_app_list()

    cache_file = tmp_path / "cache_DE737A0EDD5A.json"
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {
        "apps": {"YouTube": "com.google.ios.youtube"},
        "output_devices": {},
    }
    assert AppleTv(_device(), loop, cache_dir=tmp_path).app_names == ["YouTube"]


async def test_unchanged_app_list_is_not_written(tmp_path: Path) -> None:
    """Do not write the cache or emit an update if the app list did not change."""
    cache_file = tmp_path / "cache_DE737A0EDD5A.json"
    cache_file.write_text(json.dumps({"apps": {"YouTube": "com.google.ios.youtube"}}), encoding="utf-8")
    apple_tv = AppleTv(_device(), asyncio.get_running_loop(), cache_dir=tmp_path)
    apple_tv._atv = MagicMock()
    apple_tv._atv.apps.app_list = AsyncMock(return_value=[_app("YouTube", "com.google.ios.youtube")])
    apple_tv._store_cache = MagicMock()
    updates: list[object] = []
    apple_tv.events.on(tv.EVENTS.UPDATE, lambda *args: updates.append(args))

    await apple_tv._update_app_list()

    apple_tv._store_cache.assert_not_called()
    assert updates == []
    assert apple_tv._app_list_fetched_at > 0


async def test_output_devices_cache_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scanned output devices are written to the cache and restored with combinations by a new instance."""
    loop = asyncio.get_running_loop()
    apple_tv = AppleTv(_device(), loop, cache_dir=tmp_path)
    apple_tv._atv = MagicMock()
    apple_tv._atv.device_info.output_device_id = "atv"
    monkeypatch.setattr(tv.pyatv, "scan", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        AppleTv, "_build_id_to_name_map", staticmethod(lambda _atvs: {"atv": "Test Apple TV", "hp": "HomePod"})
    )

    await apple_tv._update_output_devices()

    data = json.loads((tmp_path / "cache_DE737A0EDD5A.json").read_text(encoding="utf-8"))
    assert data["output_devices"] == {"hp": "HomePod"}
    assert AppleTv(_device(), loop, cache_dir=tmp_path).output_devices_combinations == ["Test Apple TV", "HomePod"]


async def test_unchanged_output_devices_are_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not write the cache or emit an update if the output devices did not change."""
    (tmp_path / "cache_DE737A0EDD5A.json").write_text(
        json.dumps({"output_devices": {"hp": "HomePod"}}), encoding="utf-8"
    )
    apple_tv = AppleTv(_device(), asyncio.get_running_loop(), cache_dir=tmp_path)
    apple_tv._atv = MagicMock()
    apple_tv._atv.device_info.output_device_id = "atv"
    monkeypatch.setattr(tv.pyatv, "scan", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        AppleTv, "_build_id_to_name_map", staticmethod(lambda _atvs: {"atv": "Test Apple TV", "hp": "HomePod"})
    )
    apple_tv._store_cache = MagicMock()
    updates: list[object] = []
    apple_tv.events.on(tv.EVENTS.UPDATE, lambda *args: updates.append(args))

    await apple_tv._update_output_devices()

    apple_tv._store_cache.assert_not_called()
    assert updates == []
    assert apple_tv._output_devices_fetched_at > 0


async def test_renamed_apple_tv_updates_output_devices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new Apple TV name is shown in the output devices even if the devices did not change."""
    (tmp_path / "cache_DE737A0EDD5A.json").write_text(
        json.dumps({"output_devices": {"hp": "HomePod"}}), encoding="utf-8"
    )
    apple_tv = AppleTv(_device(), asyncio.get_running_loop(), cache_dir=tmp_path)
    apple_tv._device.name = "Living Room"
    apple_tv._atv = MagicMock()
    apple_tv._atv.device_info.output_device_id = "atv"
    monkeypatch.setattr(tv.pyatv, "scan", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        AppleTv, "_build_id_to_name_map", staticmethod(lambda _atvs: {"atv": "Living Room", "hp": "HomePod"})
    )
    updates: list[object] = []
    apple_tv.events.on(tv.EVENTS.UPDATE, lambda *args: updates.append(args))

    await apple_tv._update_output_devices()

    assert apple_tv.output_devices_combinations == ["Living Room", "HomePod"]
    assert len(updates) == 1


async def test_corrupt_cache_is_ignored(tmp_path: Path) -> None:
    """An unreadable cache file results in empty lists."""
    (tmp_path / "cache_DE737A0EDD5A.json").write_text("{not json", encoding="utf-8")

    assert AppleTv(_device(), asyncio.get_running_loop(), cache_dir=tmp_path).app_names == []


async def test_remove_cache_deletes_and_stops_writing(tmp_path: Path) -> None:
    """A removed device must not write the cache file again."""
    cache_file = tmp_path / "cache_DE737A0EDD5A.json"
    cache_file.write_text("{}", encoding="utf-8")
    apple_tv = AppleTv(_device(), asyncio.get_running_loop(), cache_dir=tmp_path)

    apple_tv.remove_cache()
    apple_tv._app_list = {"YouTube": "com.google.ios.youtube"}
    apple_tv._store_cache()

    assert not cache_file.exists()


# ---- refresh on connect ----


async def _run_connect_loop(
    apple_tv: AppleTv,
    results: list[ConnectResult] | None = None,
    *,
    disable_on_call: bool = False,
    backoff: float = 0,
    stall_timeouts: list[float | None] | None = None,
) -> int:
    """Run the connect loop with the given connect results. Return the number of connect attempts."""
    pending = iter(results or [ConnectResult.CONNECTED])
    calls = 0

    async def connect_once(stall_timeout: float | None = None) -> ConnectResult:
        nonlocal calls
        calls += 1
        if stall_timeouts is not None:
            stall_timeouts.append(stall_timeout)
        if disable_on_call:
            apple_tv._is_enabled = False
        result = next(pending)
        if result is ConnectResult.CONNECTED:
            apple_tv._atv = MagicMock()
        return result

    apple_tv._is_enabled = True
    apple_tv._connect_once = connect_once
    apple_tv._backoff = MagicMock(return_value=backoff)
    apple_tv._start_polling = AsyncMock()
    apple_tv._update_app_list = AsyncMock()
    apple_tv._update_output_devices = AsyncMock()
    await apple_tv._connect_loop()
    await asyncio.sleep(0)
    return calls


async def test_connect_skips_fresh_lists() -> None:
    """Resume from sleep does not fetch the app list and output devices again if they are fresh."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._app_list_fetched_at = time.time()
    apple_tv._output_devices_fetched_at = time.time()

    await _run_connect_loop(apple_tv)

    apple_tv._update_app_list.assert_not_awaited()
    apple_tv._update_output_devices.assert_not_awaited()


async def test_connect_without_scan_sends_current_output_device() -> None:
    """Without a scan the current output device is sent, it may have changed during sleep."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())
    apple_tv._output_devices_fetched_at = time.time()
    updates: list[dict[str, object]] = []
    apple_tv.events.on(tv.EVENTS.UPDATE, lambda _device_id, update: updates.append(update))

    await _run_connect_loop(apple_tv)

    assert {MediaAttr.SOUND_MODE: "Test Apple TV"} in updates


async def test_connect_fetches_stale_lists() -> None:
    """A first connect fetches the app list and output devices."""
    apple_tv = AppleTv(_device(), asyncio.get_running_loop())

    await _run_connect_loop(apple_tv)

    apple_tv._update_app_list.assert_awaited_once()
    apple_tv._update_output_devices.assert_awaited_once()
