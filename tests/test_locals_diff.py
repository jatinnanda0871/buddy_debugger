"""Locals-diffing on execution stops.

Every run/continue/step stop already answers "where am I" in one call. This
extends that to "what changed": the first stop in a scope returns the full
locals list, and every subsequent stop in the *same* scope reports only the
locals whose value actually moved -- so a tight step loop through a function
with many locals doesn't re-send all of them on every single step.
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
    await dbg.start(binary=__file__)  # any existing path; the fake doesn't care
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


async def test_first_stop_in_a_scope_returns_the_full_locals(debugger):
    result = await debugger.run()
    assert result["variables"] == [
        {"name": "i", "value": "0"},
        {"name": "total", "value": "42"},
    ]
    assert "changed_variables" not in result


async def test_second_stop_in_the_same_scope_reports_only_the_diff(debugger):
    await debugger.run()
    result = await debugger.step(kind="next")
    assert result["changed_variables"] == [{"name": "i", "old": "0", "new": "1"}]
    assert result["unchanged_count"] == 1
    assert "variables" not in result
