"""
Pure connection lifecycle state machine for the Apple TV integration driver.

Sans-I/O core (spec: docs/specs/001-connection-lifecycle-state-machine.md): a pure,
synchronous transition function mapping (state, event) to (new state, ordered actions).
This module MUST NOT import asyncio, pyatv or any I/O object (INV-1) — all network and
timing effects live in the async shell (`tv.py`), which feeds results back as events.

:copyright: (c) 2026 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import StrEnum

BACKOFF_SEC = 2.0
"""Backoff delay increment in seconds per failed connect attempt."""
BACKOFF_MAX = 30.0
"""Maximum backoff delay in seconds between connect attempts."""


class ConnectionState(StrEnum):
    """Connection lifecycle states."""

    STOPPED = "STOPPED"
    """Not supervised (initial, after STOP / standby)."""
    CONNECTING = "CONNECTING"
    """A connect cycle and/or backoff is in progress."""
    CONNECTED = "CONNECTED"
    """Live connection; push + poll running."""
    AUTH_FAILED = "AUTH_FAILED"
    """Credentials rejected; terminal until re-armed by an external START (INV-6)."""


class Event(StrEnum):
    """Inputs to the state machine, posted by the async shell."""

    START = "START"
    """Connect requested."""
    STOP = "STOP"
    """Disconnect requested."""
    CONNECT_SUCCEEDED = "CONNECT_SUCCEEDED"
    """The shell established a live handle."""
    CONNECT_FAILED = "CONNECT_FAILED"
    """Scan/connect/setup failed (retriable)."""
    AUTH_REJECTED = "AUTH_REJECTED"
    """Credentials rejected."""
    CONNECTION_LOST = "CONNECTION_LOST"
    """pyatv lost/closed, blocked facade, poll/command drop."""
    BACKOFF_ELAPSED = "BACKOFF_ELAPSED"
    """The scheduled retry timer fired."""


class Action(StrEnum):
    """I/O effects the shell executes, in the order returned by :meth:`ConnectionMachine.handle`."""

    START_CONNECT = "START_CONNECT"
    """Spawn a connect cycle."""
    SCHEDULE_RETRY = "SCHEDULE_RETRY"
    """Arm a backoff timer (delay = machine.backoff_delay())."""
    CANCEL_CONNECT = "CANCEL_CONNECT"
    """Cancel in-flight connect cycle + retry timer."""
    ADOPT_CONNECTION = "ADOPT_CONNECTION"
    """Promote the pending handle to the live connection."""
    TEARDOWN = "TEARDOWN"
    """Stop polling + close the live connection."""
    EMIT_CONNECTED = "EMIT_CONNECTED"
    """Emit the driver-facing connected event."""
    EMIT_DISCONNECTED = "EMIT_DISCONNECTED"
    """Emit the driver-facing disconnected event."""
    EMIT_AUTH_ERROR = "EMIT_AUTH_ERROR"
    """Emit the driver-facing authentication-error event."""


class ConnectionMachine:
    """
    Pure connection lifecycle state machine.

    :meth:`handle` is a pure, synchronous function of the current state plus the event:
    it performs no I/O, starts no tasks and never blocks (INV-1). Connect attempt
    bookkeeping (`_attempts`) is owned by the machine; :meth:`backoff_delay` derives the
    retry delay from it.

    Exactly-once edge semantics (INV-5): ``EMIT_CONNECTED`` is produced only on the
    ``CONNECTING → CONNECTED`` transition, ``EMIT_DISCONNECTED`` only on transitions
    leaving ``CONNECTED``. ``AUTH_FAILED`` is terminal until re-armed: the machine never
    schedules a retry by itself; any external ``START`` re-arms exactly one fresh
    connect cycle (INV-6).
    """

    def __init__(self, *, base_backoff: float = BACKOFF_SEC, max_backoff: float = BACKOFF_MAX) -> None:
        """
        Create a machine in the ``STOPPED`` state.

        :param base_backoff: backoff delay increment in seconds per failed attempt.
        :param max_backoff: upper bound for the backoff delay in seconds.
        """
        self._state: ConnectionState = ConnectionState.STOPPED
        self._attempts: int = 0
        self._base_backoff: float = base_backoff
        self._max_backoff: float = max_backoff

    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state

    def backoff_delay(self) -> float:
        """Current retry delay in seconds, derived from the number of failed attempts."""
        return min(self._attempts * self._base_backoff, self._max_backoff)

    def handle(self, event: Event) -> list[Action]:
        """
        Apply an event to the machine and return the ordered actions for the shell to run.

        Events that are stale or meaningless in the current state return ``[]`` and
        leave the state unchanged — the shell's FIFO serialization makes them harmless.
        """
        match self._state:
            case ConnectionState.STOPPED:
                return self._handle_stopped(event)
            case ConnectionState.CONNECTING:
                return self._handle_connecting(event)
            case ConnectionState.CONNECTED:
                return self._handle_connected(event)
            case ConnectionState.AUTH_FAILED:
                return self._handle_auth_failed(event)

    def _handle_stopped(self, event: Event) -> list[Action]:
        if event is Event.START:
            self._state = ConnectionState.CONNECTING
            self._attempts = 0
            return [Action.START_CONNECT]
        return []

    def _handle_connecting(self, event: Event) -> list[Action]:
        match event:
            case Event.CONNECT_SUCCEEDED:
                self._state = ConnectionState.CONNECTED
                self._attempts = 0
                return [Action.ADOPT_CONNECTION, Action.EMIT_CONNECTED]
            case Event.CONNECT_FAILED:
                self._attempts += 1
                return [Action.SCHEDULE_RETRY]
            case Event.BACKOFF_ELAPSED:
                return [Action.START_CONNECT]
            case Event.AUTH_REJECTED:
                self._state = ConnectionState.AUTH_FAILED
                return [Action.CANCEL_CONNECT, Action.EMIT_AUTH_ERROR]
            case Event.STOP:
                self._state = ConnectionState.STOPPED
                return [Action.CANCEL_CONNECT]
            case _:  # CONNECTION_LOST (no live conn yet) / START (already connecting)
                return []

    def _handle_connected(self, event: Event) -> list[Action]:
        match event:
            case Event.CONNECTION_LOST:
                self._state = ConnectionState.CONNECTING
                self._attempts = 0
                return [Action.TEARDOWN, Action.EMIT_DISCONNECTED, Action.START_CONNECT]
            case Event.STOP:
                self._state = ConnectionState.STOPPED
                return [Action.TEARDOWN, Action.EMIT_DISCONNECTED]
            case Event.AUTH_REJECTED:
                self._state = ConnectionState.AUTH_FAILED
                return [Action.TEARDOWN, Action.EMIT_DISCONNECTED, Action.EMIT_AUTH_ERROR]
            case _:  # START / stale I/O events
                return []

    def _handle_auth_failed(self, event: Event) -> list[Action]:
        match event:
            case Event.START:
                self._state = ConnectionState.CONNECTING
                self._attempts = 0
                return [Action.START_CONNECT]
            case Event.STOP:
                self._state = ConnectionState.STOPPED
                return []
            case _:  # terminal: no autonomous retry (INV-6)
                return []
