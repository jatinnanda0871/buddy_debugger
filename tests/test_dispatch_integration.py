"""End-to-end smoke tests for the collapsed 7-tool MCP surface.

`test_server_protocol.py` pins the isError/schema contract using backends that
always fail (no gdb on PATH, no bridge). This file complements it by actually
succeeding: driving real dispatch through `gdb_session` -> `gdb_breakpoint` ->
`gdb_exec` -> `gdb_inspect` against the fake gdb, and `vsc_session` ->
`vsc_exec` -> `vsc_inspect` against the fake bridge, over the real MCP
in-memory transport. This is what catches a field-rename mistake in the
dispatch layer (thread_id -> thread, max_results -> limit, instruction_count
-> count) that an error-path test never reaches.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session

from fake_bridge import FakeBridge
from gdb_mcp import vscode_bridge
from gdb_mcp.server import build_server
from gdb_mcp.session import GdbSession
from gdb_mcp.tools import Debugger
from gdb_mcp.vscode_bridge import VSCodeDebugger

FAKE_GDB = os.path.join(os.path.dirname(__file__), "fake_gdb.py")


class FakeGdbSession(GdbSession):
    def __init__(self, gdb_path="gdb", **kwargs):
        kwargs.pop("cwd", None)
        super().__init__(sys.executable, **kwargs)

    async def _spawn(self, mi_version):  # type: ignore[override]
        argv = [sys.executable, FAKE_GDB, f"--interpreter={mi_version}", "-q", "-nx"]
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


def payload(result):
    return json.loads(result.content[0].text)


@asynccontextmanager
async def gdb_connected(monkeypatch, tmp_path):
    # See test_server_protocol.py's module docstring: the session must be
    # entered/exited in the same task, so this is a context manager entered
    # directly inside each test, not a yield-based pytest fixture.
    import gdb_mcp.tools as tools_module

    monkeypatch.setattr(tools_module, "GdbSession", FakeGdbSession)
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "no-bridges"))
    server = build_server(Debugger(gdb_path=sys.executable), VSCodeDebugger())
    async with create_connected_server_and_client_session(server) as session:
        yield session


async def test_gdb_dispatch_chain_reaches_the_real_debugger(tmp_path, monkeypatch):
    async with gdb_connected(monkeypatch, tmp_path) as client:
        start = payload(
            await client.call_tool(
                "gdb_session", {"operation": "start", "binary": __file__}
            )
        )
        assert start["session"] == "default"

        bp = payload(
            await client.call_tool(
                "gdb_breakpoint", {"operation": "set", "location": "main"}
            )
        )
        assert bp["breakpoint"]["number"] == "1"

        run = payload(await client.call_tool("gdb_exec", {"operation": "run"}))
        assert run["state"] == "stopped"
        assert run["frame"]["func"] == "main"

        backtrace = payload(
            await client.call_tool("gdb_inspect", {"operation": "backtrace"})
        )
        assert backtrace["frames"][0]["func"] == "main"

        step = payload(await client.call_tool("gdb_exec", {"operation": "next"}))
        assert step["state"] == "stopped"

        listing = payload(
            await client.call_tool("gdb_breakpoint", {"operation": "list"})
        )
        assert len(listing["breakpoints"]) == 1

        modified = payload(
            await client.call_tool(
                "gdb_breakpoint",
                {
                    "operation": "modify",
                    "number": 1,
                    "condition": "x > 0",
                    "ignore_count": 2,
                },
            )
        )
        assert modified["breakpoint"]["cond"] == "x > 0"
        assert modified["breakpoint"]["ignore"] == "2"

        disabled = payload(
            await client.call_tool(
                "gdb_breakpoint", {"operation": "disable", "number": 1}
            )
        )
        assert disabled["enabled"] is False

        stop = payload(await client.call_tool("gdb_session", {"operation": "stop"}))
        assert stop["status"] == "closed"


async def test_gdb_inspect_field_renames_reach_the_debugger(tmp_path, monkeypatch):
    """max_results and instruction_count are dispatch-layer renames back to
    the Debugger's own `limit`/`count` kwargs -- prove they actually land."""
    async with gdb_connected(monkeypatch, tmp_path) as client:
        await client.call_tool(
            "gdb_session", {"operation": "start", "binary": __file__}
        )

        globals_result = payload(
            await client.call_tool(
                "gdb_inspect", {"operation": "globals", "max_results": 1}
            )
        )
        assert len(globals_result["globals"]) <= 1

        disasm = payload(
            await client.call_tool(
                "gdb_inspect", {"operation": "disassemble", "instruction_count": 5}
            )
        )
        assert "instructions" in disasm


@asynccontextmanager
async def vsc_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "bridges"))
    os.makedirs(vscode_bridge.BRIDGE_DIR, exist_ok=True)
    fake = FakeBridge()
    await fake.start()
    with open(os.path.join(vscode_bridge.BRIDGE_DIR, f"{os.getpid()}.json"), "w") as fh:
        json.dump(fake.discovery_info(), fh)
    server = build_server(Debugger(gdb_path="gdb-does-not-exist"), VSCodeDebugger())
    try:
        async with create_connected_server_and_client_session(server) as session:
            yield fake, session
    finally:
        await fake.stop()


async def test_vsc_dispatch_chain_reaches_the_real_bridge(tmp_path, monkeypatch):
    async with vsc_connected(tmp_path, monkeypatch) as (fake, client):
        fake.add_session()
        fake.set_stopped(reason="breakpoint", thread_id=3)
        fake.dap_responses["stackTrace"] = {
            "stackFrames": [{"id": 9, "name": "handle_packet", "source": {}, "line": 5}]
        }
        fake.dap_responses["scopes"] = {"scopes": []}

        status = payload(await client.call_tool("vsc_session", {"operation": "status"}))
        assert status["state"] == "stopped"
        assert status["frame"]["name"] == "handle_packet"

        backtrace = payload(
            await client.call_tool("vsc_inspect", {"operation": "backtrace"})
        )
        assert backtrace["frames"][0]["name"] == "handle_packet"

        fake.dap_responses["evaluate"] = {"result": "0x2a"}
        eval_result = payload(
            await client.call_tool(
                "vsc_inspect", {"operation": "eval", "expression": "x", "frame_id": 9}
            )
        )
        assert eval_result["value"] == "0x2a"
