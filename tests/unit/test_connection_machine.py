"""
Bucket A — exhaustive unit tests for the pure ConnectionMachine.

No mocks, no event loop, no network (spec 001, INV-1/AC-1/AC-2): every test constructs
a machine, drives it with plain synchronous `handle()` calls and asserts the resulting
state and ordered action list. Covers every (state, event) cell of the transition table
plus the scenario rows A-HAPPY … A-PURITY.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import ast
import itertools
from pathlib import Path

import pytest

from connection_machine import BACKOFF_MAX, BACKOFF_SEC, Action, ConnectionMachine, ConnectionState, Event


def machine_in(state: ConnectionState) -> ConnectionMachine:
    """Drive a fresh machine to the given state via regular transitions."""
    machine = ConnectionMachine()
    if state is ConnectionState.STOPPED:
        return machine
    machine.handle(Event.START)
    if state is ConnectionState.CONNECTING:
        return machine
    if state is ConnectionState.CONNECTED:
        machine.handle(Event.CONNECT_SUCCEEDED)
        return machine
    machine.handle(Event.AUTH_REJECTED)
    assert machine.state is ConnectionState.AUTH_FAILED
    return machine


# --- Exhaustive transition table -------------------------------------------------
# One entry per (state, event) cell, matching the authoritative table in spec 001.

TRANSITION_TABLE: dict[tuple[ConnectionState, Event], tuple[ConnectionState, list[Action]]] = {
    # STOPPED
    (ConnectionState.STOPPED, Event.START): (ConnectionState.CONNECTING, [Action.START_CONNECT]),
    (ConnectionState.STOPPED, Event.STOP): (ConnectionState.STOPPED, []),
    (ConnectionState.STOPPED, Event.CONNECT_SUCCEEDED): (ConnectionState.STOPPED, []),
    (ConnectionState.STOPPED, Event.CONNECT_FAILED): (ConnectionState.STOPPED, []),
    (ConnectionState.STOPPED, Event.AUTH_REJECTED): (ConnectionState.STOPPED, []),
    (ConnectionState.STOPPED, Event.CONNECTION_LOST): (ConnectionState.STOPPED, []),
    (ConnectionState.STOPPED, Event.BACKOFF_ELAPSED): (ConnectionState.STOPPED, []),
    # CONNECTING
    (ConnectionState.CONNECTING, Event.START): (ConnectionState.CONNECTING, []),
    (ConnectionState.CONNECTING, Event.STOP): (ConnectionState.STOPPED, [Action.CANCEL_CONNECT]),
    (ConnectionState.CONNECTING, Event.CONNECT_SUCCEEDED): (
        ConnectionState.CONNECTED,
        [Action.ADOPT_CONNECTION, Action.EMIT_CONNECTED],
    ),
    (ConnectionState.CONNECTING, Event.CONNECT_FAILED): (ConnectionState.CONNECTING, [Action.SCHEDULE_RETRY]),
    (ConnectionState.CONNECTING, Event.AUTH_REJECTED): (
        ConnectionState.AUTH_FAILED,
        [Action.CANCEL_CONNECT, Action.EMIT_AUTH_ERROR],
    ),
    (ConnectionState.CONNECTING, Event.CONNECTION_LOST): (ConnectionState.CONNECTING, []),
    (ConnectionState.CONNECTING, Event.BACKOFF_ELAPSED): (ConnectionState.CONNECTING, [Action.START_CONNECT]),
    # CONNECTED
    (ConnectionState.CONNECTED, Event.START): (ConnectionState.CONNECTED, []),
    (ConnectionState.CONNECTED, Event.STOP): (
        ConnectionState.STOPPED,
        [Action.TEARDOWN, Action.EMIT_DISCONNECTED],
    ),
    (ConnectionState.CONNECTED, Event.CONNECT_SUCCEEDED): (ConnectionState.CONNECTED, []),
    (ConnectionState.CONNECTED, Event.CONNECT_FAILED): (ConnectionState.CONNECTED, []),
    (ConnectionState.CONNECTED, Event.AUTH_REJECTED): (
        ConnectionState.AUTH_FAILED,
        [Action.TEARDOWN, Action.EMIT_DISCONNECTED, Action.EMIT_AUTH_ERROR],
    ),
    (ConnectionState.CONNECTED, Event.CONNECTION_LOST): (
        ConnectionState.CONNECTING,
        [Action.TEARDOWN, Action.EMIT_DISCONNECTED, Action.START_CONNECT],
    ),
    (ConnectionState.CONNECTED, Event.BACKOFF_ELAPSED): (ConnectionState.CONNECTED, []),
    # AUTH_FAILED
    (ConnectionState.AUTH_FAILED, Event.START): (ConnectionState.CONNECTING, [Action.START_CONNECT]),
    (ConnectionState.AUTH_FAILED, Event.STOP): (ConnectionState.STOPPED, []),
    (ConnectionState.AUTH_FAILED, Event.CONNECT_SUCCEEDED): (ConnectionState.AUTH_FAILED, []),
    (ConnectionState.AUTH_FAILED, Event.CONNECT_FAILED): (ConnectionState.AUTH_FAILED, []),
    (ConnectionState.AUTH_FAILED, Event.AUTH_REJECTED): (ConnectionState.AUTH_FAILED, []),
    (ConnectionState.AUTH_FAILED, Event.CONNECTION_LOST): (ConnectionState.AUTH_FAILED, []),
    (ConnectionState.AUTH_FAILED, Event.BACKOFF_ELAPSED): (ConnectionState.AUTH_FAILED, []),
}


def test_transition_table_is_exhaustive() -> None:
    """Every (state, event) combination has exactly one expected cell."""
    assert set(TRANSITION_TABLE) == {(s, e) for s in ConnectionState for e in Event}


@pytest.mark.parametrize(("state", "event"), sorted(TRANSITION_TABLE, key=str))
def test_transition_cell(state: ConnectionState, event: Event) -> None:
    """Each cell yields exactly the state and ordered actions from the spec table."""
    expected_state, expected_actions = TRANSITION_TABLE[(state, event)]
    machine = machine_in(state)
    actions = machine.handle(event)
    assert actions == expected_actions
    assert machine.state is expected_state


# --- Scenario rows (spec 001, Bucket A) ------------------------------------------


def test_a_happy() -> None:
    """A-HAPPY: STOPPED —START→ CONNECTING —CONNECT_SUCCEEDED→ CONNECTED."""
    machine = ConnectionMachine()
    assert machine.handle(Event.START) == [Action.START_CONNECT]
    assert machine.state is ConnectionState.CONNECTING
    assert machine.handle(Event.CONNECT_SUCCEEDED) == [Action.ADOPT_CONNECTION, Action.EMIT_CONNECTED]
    assert machine.state is ConnectionState.CONNECTED


def test_a_backoff_increases_then_caps() -> None:
    """A-BACKOFF: repeated CONNECT_FAILED grows the delay linearly up to the cap."""
    machine = machine_in(ConnectionState.CONNECTING)
    assert machine.backoff_delay() == 0.0

    delays: list[float] = []
    for _ in range(30):
        assert machine.handle(Event.CONNECT_FAILED) == [Action.SCHEDULE_RETRY]
        assert machine.state is ConnectionState.CONNECTING
        delays.append(machine.backoff_delay())

    assert delays[0] == BACKOFF_SEC
    assert delays[1] == 2 * BACKOFF_SEC
    assert all(later >= earlier for earlier, later in itertools.pairwise(delays))
    assert delays[-1] == BACKOFF_MAX
    assert max(delays) == BACKOFF_MAX


def test_a_backoff_resets_after_success() -> None:
    """A-BACKOFF: delay resets to base after CONNECT_SUCCEEDED."""
    machine = machine_in(ConnectionState.CONNECTING)
    for _ in range(5):
        machine.handle(Event.CONNECT_FAILED)
    assert machine.backoff_delay() > 0.0

    machine.handle(Event.CONNECT_SUCCEEDED)
    assert machine.state is ConnectionState.CONNECTED
    assert machine.backoff_delay() == 0.0


def test_backoff_resets_on_fresh_connecting() -> None:
    """Entering CONNECTING fresh (STOP→START, AUTH re-arm, CONNECTION_LOST) resets attempts."""
    for enter_fresh in (
        [Event.STOP, Event.START],  # STOPPED → START
        [Event.AUTH_REJECTED, Event.START],  # AUTH_FAILED → START
        [Event.CONNECT_SUCCEEDED, Event.CONNECTION_LOST],  # CONNECTED → CONNECTION_LOST
    ):
        machine = machine_in(ConnectionState.CONNECTING)
        for _ in range(5):
            machine.handle(Event.CONNECT_FAILED)
        assert machine.backoff_delay() > 0.0
        for event in enter_fresh:
            machine.handle(event)
        assert machine.state is ConnectionState.CONNECTING
        assert machine.backoff_delay() == 0.0


def test_a_lost() -> None:
    """A-LOST: CONNECTED —CONNECTION_LOST→ CONNECTING with teardown + emit + reconnect."""
    machine = machine_in(ConnectionState.CONNECTED)
    actions = machine.handle(Event.CONNECTION_LOST)
    assert actions == [Action.TEARDOWN, Action.EMIT_DISCONNECTED, Action.START_CONNECT]
    assert machine.state is ConnectionState.CONNECTING


def test_a_dup_lost_emits_disconnected_once() -> None:
    """A-DUP-LOST: a duplicate CONNECTION_LOST yields [] — exactly one EMIT_DISCONNECTED (INV-5, F2)."""
    machine = machine_in(ConnectionState.CONNECTED)
    first = machine.handle(Event.CONNECTION_LOST)
    second = machine.handle(Event.CONNECTION_LOST)
    assert second == []
    assert (first + second).count(Action.EMIT_DISCONNECTED) == 1
    assert machine.state is ConnectionState.CONNECTING


def test_a_auth_terminal() -> None:
    """A-AUTH: AUTH_REJECTED is terminal — no autonomous retry actions afterwards (INV-6)."""
    machine = machine_in(ConnectionState.CONNECTING)
    actions = machine.handle(Event.AUTH_REJECTED)
    assert actions == [Action.CANCEL_CONNECT, Action.EMIT_AUTH_ERROR]
    assert machine.state is ConnectionState.AUTH_FAILED

    for event in (Event.BACKOFF_ELAPSED, Event.CONNECT_FAILED, Event.CONNECTION_LOST, Event.CONNECT_SUCCEEDED):
        assert machine.handle(event) == []
        assert machine.state is ConnectionState.AUTH_FAILED


def test_a_auth_rearm() -> None:
    """A-AUTH-REARM: START re-arms exactly one fresh connect cycle from AUTH_FAILED."""
    machine = machine_in(ConnectionState.AUTH_FAILED)
    assert machine.handle(Event.START) == [Action.START_CONNECT]
    assert machine.state is ConnectionState.CONNECTING


def test_a_stop_connecting() -> None:
    """A-STOP-CONNECTING: STOP while connecting cancels without EMIT_DISCONNECTED."""
    machine = machine_in(ConnectionState.CONNECTING)
    actions = machine.handle(Event.STOP)
    assert actions == [Action.CANCEL_CONNECT]
    assert Action.EMIT_DISCONNECTED not in actions
    assert machine.state is ConnectionState.STOPPED


def test_a_bounce() -> None:
    """A-BOUNCE: STOP then immediate START (standby bounce) ends deterministic in CONNECTING (F7)."""
    machine = machine_in(ConnectionState.CONNECTED)
    machine.handle(Event.STOP)
    assert machine.state is ConnectionState.STOPPED
    assert machine.handle(Event.START) == [Action.START_CONNECT]
    assert machine.state is ConnectionState.CONNECTING


def test_a_stale_events_are_noops() -> None:
    """A-STALE: stale CONNECT_SUCCEEDED / BACKOFF_ELAPSED in STOPPED / CONNECTED do nothing."""
    for state in (ConnectionState.STOPPED, ConnectionState.CONNECTED):
        for event in (Event.CONNECT_SUCCEEDED, Event.BACKOFF_ELAPSED):
            machine = machine_in(state)
            assert machine.handle(event) == []
            assert machine.state is state


def test_a_purity_no_asyncio_no_pyatv_imports() -> None:
    """A-PURITY: connection_machine imports neither asyncio nor pyatv nor any I/O module (INV-1)."""
    import connection_machine

    banned = {"asyncio", "pyatv", "socket", "ucapi"}
    tree = ast.parse(Path(connection_machine.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not (imported & banned), f"forbidden imports in pure core: {imported & banned}"
