"""Reverse execution: gdb_record (start/stop recording) and gdb_reverse
(reverse-continue/step/next/finish), driven against tests/fake_gdb.py.

Real gdb only accepts `--reverse` execution commands while a process record is
active; the fake mirrors that (see `_recording` in fake_gdb.py) so these tests
cover both the happy path and the "you forgot to start recording" error.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from gdb_mcp.session import GdbError, GdbSession
from gdb_mcp.tools import Debugger

FAKE = os.path.join(os.path.dirname(__file__), "fake_gdb.py")


class FakeGdbSession(GdbSession):
    """GdbSession that launches the fake MI process instead of real gdb."""

    def __init__(self, gdb_path="gdb", **kwargs):
        kwargs.pop("cwd", None)
        super().__init__(sys.executable, **kwargs)

    async def _spawn(self, mi_version):  # type: ignore[override]
        argv = [sys.executable, FAKE, f"--interpreter={mi_version}", "-q", "-nx"]
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        for command in ("-gdb-set confirm off", "-gdb-set pagination off"):
            await self.send(command, timeout=10)


@pytest.fixture
async def debugger(monkeypatch):
    import gdb_mcp.tools as tools_module

    monkeypatch.setattr(tools_module, "GdbSession", FakeGdbSession)
    dbg = Debugger(gdb_path=sys.executable)
    await dbg.start(binary=__file__)
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


async def test_reverse_without_recording_gives_a_clear_error(debugger):
    with pytest.raises(GdbError, match="gdb_record"):
        await debugger.reverse(kind="continue")


async def test_record_start_then_reverse_continue_stops(debugger):
    result = await debugger.record(action="start")
    assert result["recording"] is True

    stop = await debugger.reverse(kind="continue")
    assert stop["state"] == "stopped"
    assert stop["frame"]["line"] == "4"


async def test_record_stop_disables_reverse_again(debugger):
    await debugger.record(action="start")
    await debugger.record(action="stop")
    with pytest.raises(GdbError, match="gdb_record"):
        await debugger.reverse(kind="step")


async def test_unknown_reverse_kind_is_rejected(debugger):
    await debugger.record(action="start")
    with pytest.raises(ValueError, match="Unknown reverse kind"):
        await debugger.reverse(kind="teleport")


async def test_unknown_record_action_is_rejected(debugger):
    with pytest.raises(ValueError, match="action must be"):
        await debugger.record(action="pause")
