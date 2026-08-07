"""The vsc_* backend against the real VS Code bridge extension.

Requires a VS Code window running the extension AND an explicit opt-in:

    GDB_MCP_LIVE_BRIDGE=1 pytest tests/live/test_live_bridge.py

These start and stop debug sessions in your editor, which is why merely having
VS Code open is not enough to trigger them.

The target is Python (debugpy) because that adapter is installable anywhere.
That covers all the plain-DAP paths; the cppdbg-only tools (vsc_exec,
vsc_globals) are covered here just by asserting they refuse cleanly.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.live

BREAKPOINT_LINE = 22  # `total = sum(scratch)` in target.py -- reachable


def debugpy_config(program: str) -> dict:
    return {
        "name": "gdb-mcp live test",
        "type": "debugpy",
        "request": "launch",
        "program": program,
        "console": "internalConsole",
        "internalConsoleOptions": "neverOpen",
        "justMyCode": True,
    }


@pytest.fixture
async def paused(live_bridge, python_target):
    """Launch the target and stop at a breakpoint; always tear the session down."""
    await live_bridge.set_breakpoints([], action="clear")
    await live_bridge.set_breakpoints(
        [{"file": python_target, "line": BREAKPOINT_LINE}]
    )
    launched = await live_bridge.launch(
        config=debugpy_config(python_target), wait_for_stop=True, timeout=120
    )
    assert launched["stop"]["state"] == "stopped", launched
    try:
        yield live_bridge, launched["stop"]
    finally:
        await live_bridge.set_breakpoints([], action="clear")
        try:
            await live_bridge.terminate()
        except Exception:  # noqa: BLE001 - teardown must not mask failures
            pass
        await asyncio.sleep(0.3)


# -- discovery ----------------------------------------------------------------


async def test_bridge_is_discoverable(live_bridge):
    from gdb_mcp.vscode_bridge import discover

    info = discover()
    assert info["transport"] in ("unix", "tcp")
    assert info["token"]
    if info["transport"] == "unix":
        assert info["socketPath"]
    else:
        assert info["port"]


async def test_status_without_a_session_says_so(live_bridge):
    status = await live_bridge.status()
    assert status["bridge"] == "connected"
    if status["state"] == "no active debug session":
        assert "vsc_launch" in status["hint"]


async def test_launch_configurations_can_be_listed(live_bridge):
    result = await live_bridge.configs()
    assert "workspaces" in result


# -- a real debug session -----------------------------------------------------


async def test_launch_stops_at_the_breakpoint(paused):
    _bridge, stop = paused
    assert stop["reason"] in ("breakpoint", "step")
    assert stop["frame"]["name"] == "parse_header"
    assert stop["frame"]["line"] == BREAKPOINT_LINE


async def test_stop_carries_source_context(paused):
    _bridge, stop = paused
    assert stop["source"], "no source context"
    assert any(line["current"] for line in stop["source"])


async def test_backtrace_shows_the_call_chain(paused):
    bridge, _stop = paused
    result = await bridge.backtrace(limit=10)
    names = [frame["name"] for frame in result["frames"]]
    assert "parse_header" in names
    assert "handle_packet" in names


async def test_threads_are_listed(paused):
    bridge, _stop = paused
    result = await bridge.threads()
    assert len(result["threads"]) >= 1


async def test_frame_exposes_locals(paused):
    bridge, _stop = paused
    frames = (await bridge.backtrace(limit=1))["frames"]
    frame = await bridge.frame(frames[0]["id"], expand_depth=1)
    names = {
        var["name"] for scope in frame["scopes"] for var in scope.get("variables", [])
    }
    assert {"raw", "limit"} & names, names


async def test_nested_values_expand_one_level(paused):
    bridge, _stop = paused
    frames = (await bridge.backtrace(limit=1))["frames"]
    frame = await bridge.frame(frames[0]["id"], expand_depth=1)
    expanded = [
        var
        for scope in frame["scopes"]
        for var in scope.get("variables", [])
        if var.get("children")
    ]
    assert expanded, "nothing was expanded"


async def test_expressions_evaluate_in_the_stopped_frame(paused):
    bridge, _stop = paused
    assert "4096" in (await bridge.evaluate("limit"))["value"]
    assert "11" in (await bridge.evaluate("len(raw)"))["value"]


# -- the async-stop contract, live --------------------------------------------


async def test_step_stops_again(paused):
    bridge, stop = paused
    result = await bridge.resume("next", timeout=30)
    assert result["state"] == "stopped"
    assert result["frame"]["line"] != stop["frame"]["line"]


async def test_continue_runs_to_completion(paused, python_target):
    bridge, _stop = paused
    await bridge.set_breakpoints([], action="clear")
    result = await bridge.resume("continue", timeout=60)
    assert result["state"] in ("exited", "terminated")


async def test_program_output_is_captured(paused):
    bridge, _stop = paused
    await bridge.set_breakpoints([], action="clear")
    result = await bridge.resume("continue", timeout=60)
    combined = result.get("program_output", "")
    assert "ok:" in combined or "target starting" in combined


# -- adapter-capability guards ------------------------------------------------


async def test_exec_refuses_on_a_non_gdb_adapter(paused):
    """debugpy has no `-exec`; the refusal must name a way forward."""
    from gdb_mcp.vscode_bridge import BridgeError

    bridge, _stop = paused
    with pytest.raises(BridgeError, match="GDB-backed"):
        await bridge.exec_gdb("info registers")


async def test_globals_refuses_on_a_non_gdb_adapter(paused):
    from gdb_mcp.vscode_bridge import BridgeError

    bridge, _stop = paused
    with pytest.raises(BridgeError, match="GDB-backed"):
        await bridge.list_globals(pattern="g_")


async def test_inspecting_after_the_session_ends_is_explained(paused):
    """A bare DAP "Canceled" tells an agent nothing."""
    from gdb_mcp.vscode_bridge import BridgeError

    bridge, _stop = paused
    await bridge.set_breakpoints([], action="clear")
    await bridge.resume("continue", timeout=60)
    await asyncio.sleep(0.5)
    try:
        await bridge.backtrace()
    except BridgeError as exc:
        assert "session" in str(exc).lower()
