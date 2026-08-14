"""Editing an existing breakpoint's condition/ignore count in place.

Deleting and recreating a breakpoint to change its condition loses its
number and accumulated hit count; gdb_modify_breakpoint changes it directly
via -break-condition / -break-after (verified against real gdb 15.1 -- see
tests/live/test_live_gdb.py for the real-gdb round trip).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from gdb_mcp.session import GdbSession
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
    await dbg.set_breakpoint("main")
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


async def test_setting_a_condition_does_not_change_the_breakpoint_number(debugger):
    result = await debugger.modify_breakpoint(1, condition="x > 0")
    assert result["breakpoint"]["number"] == "1"
    assert result["breakpoint"]["cond"] == "x > 0"


async def test_empty_string_clears_the_condition(debugger):
    await debugger.modify_breakpoint(1, condition="x > 0")
    result = await debugger.modify_breakpoint(1, condition="")
    assert "cond" not in result["breakpoint"]


async def test_ignore_count_is_set_without_touching_condition(debugger):
    await debugger.modify_breakpoint(1, condition="x > 0")
    result = await debugger.modify_breakpoint(1, ignore_count=5)
    assert result["breakpoint"]["ignore"] == "5"
    assert result["breakpoint"]["cond"] == "x > 0"


async def test_neither_argument_is_rejected(debugger):
    with pytest.raises(ValueError, match="nothing to change"):
        await debugger.modify_breakpoint(1)
