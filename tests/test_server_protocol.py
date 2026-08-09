"""End-to-end MCP protocol tests, over the SDK's in-memory transport.

These drive the real server the way a client does: initialize, list tools, call
tools.  They exist mainly to pin the `isError` contract -- a debugger whose
failures look like successes is worse than one that fails loudly.

The surface is 7 dispatcher tools (`gdb_session`, `gdb_exec`, `gdb_breakpoint`,
`gdb_inspect`, `vsc_session`, `vsc_exec`, `vsc_inspect`), each taking an
`operation` field. Calls below always include one.

Note the session is entered with `async with` inside each test rather than via
a yield fixture: the SDK's session is built on anyio task groups, which must be
exited in the task that entered them, and pytest-asyncio runs fixture setup and
teardown in different tasks.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from gdb_mcp import vscode_bridge
from gdb_mcp.server import ALL_TOOLS, build_server
from gdb_mcp.tools import Debugger
from gdb_mcp.vscode_bridge import VSCodeDebugger


@asynccontextmanager
async def connected(tmp_path, monkeypatch):
    # Point bridge discovery at an empty directory and gdb at a name that is
    # not on PATH, so both backends fail predictably rather than picking up
    # whatever happens to be installed on the machine running the tests.
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "no-bridges"))
    server = build_server(Debugger(gdb_path="gdb-does-not-exist"), VSCodeDebugger())
    async with create_connected_server_and_client_session(server) as session:
        yield session


def payload(result):
    return json.loads(result.content[0].text)


async def test_initialize_and_list_tools(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        listed = await client.list_tools()
        assert len(listed.tools) == len(ALL_TOOLS) == 7
        names = {t.name for t in listed.tools}
        assert names == {
            "gdb_session",
            "gdb_exec",
            "gdb_breakpoint",
            "gdb_inspect",
            "vsc_session",
            "vsc_exec",
            "vsc_inspect",
        }


async def test_every_tool_has_a_description_and_object_schema(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        for tool in (await client.list_tools()).tools:
            assert tool.description, tool.name
            assert tool.inputSchema.get("type") == "object", tool.name
            assert "properties" in tool.inputSchema, tool.name
            assert "operation" in tool.inputSchema["properties"], tool.name


# -- the isError contract -----------------------------------------------------


async def test_runtime_failure_sets_is_error(tmp_path, monkeypatch):
    """A missing gdb is a failure, and the client must be able to see that."""
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_session", {"operation": "start"})
        assert result.isError is True
        assert "not found on PATH" in payload(result)["error"]


async def test_missing_bridge_sets_is_error(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("vsc_session", {"operation": "status"})
        assert result.isError is True
        assert "GDB MCP Bridge extension" in payload(result)["error"]


async def test_missing_session_sets_is_error(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_session", {"operation": "status"})
        assert result.isError is True
        assert "No active GDB session" in payload(result)["error"]


async def test_successful_call_does_not_set_is_error(tmp_path, monkeypatch):
    """stop on an absent session is a no-op, not a failure."""
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_session", {"operation": "stop"})
        assert result.isError is False
        assert payload(result)["status"] == "no such session"


async def test_breakpoint_enable_and_disable_reach_dispatch(tmp_path, monkeypatch):
    """enable/disable are Debugger.enable_breakpoint(enabled=True/False) under
    one operation pair -- pin that both actually route there, not just one."""
    async with connected(tmp_path, monkeypatch) as client:
        for operation in ("enable", "disable"):
            result = await client.call_tool(
                "gdb_breakpoint", {"operation": operation, "number": 1}
            )
            assert result.isError is True
            assert "No active GDB session" in payload(result)["error"]


async def test_breakpoint_modify_reaches_dispatch(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "gdb_breakpoint", {"operation": "modify", "number": 1, "condition": "x > 0"}
        )
        assert result.isError is True
        assert "No active GDB session" in payload(result)["error"]


async def test_record_operation_reaches_dispatch(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_session", {"operation": "record"})
        assert result.isError is True
        assert "No active GDB session" in payload(result)["error"]


async def test_reverse_exec_operation_reaches_dispatch(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_exec", {"operation": "reverse-continue"})
        assert result.isError is True
        assert "No active GDB session" in payload(result)["error"]


async def test_unknown_tool_sets_is_error(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_not_a_tool", {"operation": "status"})
        assert result.isError is True


async def test_error_payload_names_the_failing_tool(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_session", {"operation": "start"})
        assert payload(result)["tool"] == "gdb_session"


# -- input validation ---------------------------------------------------------


async def test_schema_violation_is_rejected_before_dispatch(tmp_path, monkeypatch):
    """word_size is constrained to whole words; the SDK enforces the enum."""
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "gdb_inspect", {"operation": "memory", "address": "0x1", "word_size": 1}
        )
        assert result.isError is True
        assert "validation" in result.content[0].text.lower()


async def test_missing_required_argument_is_rejected(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("gdb_inspect", {"operation": "memory"})
        assert result.isError is True
        assert "required" in result.content[0].text.lower()


async def test_conditional_required_field_depends_on_operation(tmp_path, monkeypatch):
    """`location` is only required when operation=set -- the allOf/if/then
    branch, not a flat per-tool requirement, must be what's enforced here."""
    async with connected(tmp_path, monkeypatch) as client:
        missing = await client.call_tool("gdb_breakpoint", {"operation": "set"})
        assert missing.isError is True
        assert "required" in missing.content[0].text.lower()

        # the same tool's `list` operation has no required fields at all
        listing = await client.call_tool("gdb_breakpoint", {"operation": "list"})
        assert listing.isError is True  # no session yet, but past schema validation
        assert "No active GDB session" in payload(listing)["error"]


async def test_wrong_argument_type_is_rejected(tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "gdb_inspect", {"operation": "memory", "address": "0x1", "count": "lots"}
        )
        assert result.isError is True


async def test_unexpected_argument_does_not_crash_the_server(tmp_path, monkeypatch):
    """Schemas don't set additionalProperties:false, so junk reaches dispatch.

    Operations that forward **args turn it into a TypeError, which must come
    back as a clean error rather than an unhandled crash.
    """
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "gdb_inspect", {"operation": "backtrace", "bogus_argument": 1}
        )
        assert result.isError is True
        assert "bad arguments" in payload(result)["error"]
        # the session must still be usable afterwards
        assert (await client.list_tools()).tools


# -- negative coverage across the full tool surface ---------------------------
#
# test_server_schema.py already proves every operation's required-field
# enforcement exhaustively at the schema level. These prove the same
# rejections actually surface correctly through the real call_tool() handler
# for every one of the 7 tools -- the wiring, not just the schema fragment.


ALL_TOOL_NAMES = [t.name for t in ALL_TOOLS]


@pytest.mark.parametrize("tool", ALL_TOOL_NAMES)
async def test_unknown_operation_is_rejected_for_every_tool(tool, tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(tool, {"operation": "__bogus__"})
        assert result.isError is True


@pytest.mark.parametrize("tool", ALL_TOOL_NAMES)
async def test_missing_operation_is_rejected_for_every_tool(tool, tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(tool, {})
        assert result.isError is True
        assert "required" in result.content[0].text.lower()


async def test_unexpected_argument_on_a_vsc_operation_does_not_crash_the_server(
    tmp_path, monkeypatch
):
    """The gdb_inspect case above proves this for the gdb_* side; vsc_* goes
    through a separate dispatch function with its own **args forwarding."""
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "vsc_session", {"operation": "launch", "name": "x", "bogus_argument": 1}
        )
        assert result.isError is True
        assert "bad arguments" in payload(result)["error"]
        assert (await client.list_tools()).tools


async def test_session_field_leaking_into_a_vsc_call_does_not_crash(tmp_path, monkeypatch):
    """vsc_* tools don't declare `session` (there's only one VS Code session),
    but nothing stops a client from sending it anyway; it must not reach a
    VSCodeDebugger method that doesn't accept it and blow up uncleanly."""
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool(
            "vsc_session", {"operation": "launch", "name": "x", "session": "default"}
        )
        assert result.isError is True
        assert "bad arguments" in payload(result)["error"]


# -- launch failures ----------------------------------------------------------


async def test_absolute_missing_gdb_path_is_a_clean_error(tmp_path, monkeypatch):
    """An absolute path skips the PATH check, so the spawn itself must be guarded."""
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "no-bridges"))
    missing = str(tmp_path / "nowhere" / "gdb")
    server = build_server(Debugger(gdb_path=missing), VSCodeDebugger())
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("gdb_session", {"operation": "start"})
        assert result.isError is True
        assert "could not launch" in payload(result)["error"]


@pytest.mark.parametrize("operation", ["backtrace", "threads", "output"])
async def test_vscode_tools_fail_clearly_without_a_bridge(operation, tmp_path, monkeypatch):
    async with connected(tmp_path, monkeypatch) as client:
        result = await client.call_tool("vsc_inspect", {"operation": operation})
        assert result.isError is True
        assert "Bridge extension" in payload(result)["error"]
