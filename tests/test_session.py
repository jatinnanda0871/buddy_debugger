"""Session-layer tests driven against tests/fake_gdb.py.

These validate the parts that are easy to get subtly wrong: matching results to
the command that asked for them, capturing console output per command, and
treating `^running` as "not stopped yet" rather than "done".
"""

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
        # Absolute path, so start()'s PATH check passes.
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
async def session():
    s = FakeGdbSession()
    await s.start()
    try:
        yield s
    finally:
        await s.close(kill=False)


async def test_starts_and_answers(session):
    result = await session.send("-file-exec-and-symbols /tmp/t")
    assert result.message == "done"


async def test_result_records_match_their_command(session):
    """Tokens must route each result back to its own caller."""
    a, b, c = await asyncio.gather(
        session.send("-data-evaluate-expression x"),
        session.send("-break-insert main"),
        session.send("-stack-list-frames 0 29"),
    )
    assert a.payload["value"] == "42"
    assert b.payload["bkpt"]["number"] == "1"
    assert c.payload["stack"][0]["func"] == "main"


async def test_error_raises_gdberror(session):
    with pytest.raises(GdbError) as excinfo:
        await session.send("-data-evaluate-expression boom")
    assert "No symbol" in str(excinfo.value)


async def test_error_can_be_returned_instead_of_raised(session):
    result = await session.send(
        "-data-evaluate-expression boom", raise_on_error=False
    )
    assert result.record.is_error


async def test_console_output_is_captured_and_log_separated(session):
    result = await session.send('-interpreter-exec console "info locals"')
    assert "console line one" in result.console
    assert "console line two" in result.console
    assert "log noise" in result.log
    assert "log noise" not in result.console


async def test_console_buffer_does_not_leak_between_commands(session):
    await session.send('-interpreter-exec console "info locals"')
    result = await session.send("-break-insert main")
    assert result.console == ""


async def test_execute_waits_for_the_async_stop(session):
    """The core contract: -exec-run returns ^running, the stop comes later."""
    result, stop = await session.execute("-exec-run", stop_timeout=5)
    assert result.message == "running"
    assert stop is not None
    assert stop.reason == "breakpoint-hit"
    assert stop.payload["frame"]["func"] == "main"
    assert session.running is False


async def test_running_program_reports_no_stop_on_timeout(session):
    """A program that never stops must not fake a stop event."""
    result, stop = await session.execute("-exec-continue", stop_timeout=0.3)
    assert result.message == "running"
    assert stop is None
    assert session.running is True


async def test_interrupt_halts_a_running_program(session):
    await session.execute("-exec-continue", stop_timeout=0.2)
    assert session.running is True
    stop = await session.interrupt()
    assert stop is not None
    assert stop.reason == "signal-received"
    assert stop.payload["signal-name"] == "SIGINT"
    assert session.running is False


async def test_stale_stops_are_drained_before_next_execute(session):
    await session.execute("-exec-run", stop_timeout=5)
    # A second run must wait for its own stop, not reuse the previous one.
    _result, stop = await session.execute("-exec-step", stop_timeout=5)
    assert stop.reason == "end-stepping-range"
    assert stop.payload["frame"]["line"] == "6"


async def test_close_is_idempotent(session):
    await session.close(kill=False)
    await session.close(kill=False)
    assert session.alive is False


# -- Debugger layer -----------------------------------------------------------


@pytest.fixture
async def debugger(monkeypatch):
    monkeypatch.setattr("gdb_mcp.tools.GdbSession", FakeGdbSession)
    dbg = Debugger()
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


async def test_debugger_requires_start_first(debugger):
    with pytest.raises(ValueError, match="No active GDB session"):
        await debugger.status()


async def test_debugger_rejects_double_start(debugger):
    await debugger.start()
    with pytest.raises(ValueError, match="already running"):
        await debugger.start()


async def test_debugger_stop_reports_stop_details(debugger, tmp_path):
    binary = tmp_path / "t"
    binary.write_bytes(b"\x7fELF")
    await debugger.start(binary=str(binary))
    out = await debugger.run(stop_at_main=False, timeout=5)
    assert out["state"] == "stopped"
    assert out["reason"] == "breakpoint-hit"
    assert out["frame"]["func"] == "main"
    assert out["breakpoint"] == "1"


async def test_debugger_reports_still_running_rather_than_lying(debugger, tmp_path):
    binary = tmp_path / "t"
    binary.write_bytes(b"\x7fELF")
    await debugger.start(binary=str(binary))
    await debugger.run(stop_at_main=False, timeout=5)
    out = await debugger.cont(timeout=0.3)
    assert out["state"] == "running"
    assert "gdb_interrupt" in out["note"]


async def test_debugger_missing_binary_is_a_clear_error(debugger):
    await debugger.start()
    with pytest.raises(ValueError, match="Binary not found"):
        await debugger.load_binary("/nonexistent/path/xyz")


async def test_eval_truncates_and_returns_value(debugger):
    await debugger.start()
    out = await debugger.evaluate("x")
    assert out["value"] == "42"
