"""Tests for the VS Code bridge client, driven against tests/fake_bridge.py."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from fake_bridge import FakeBridge
from gdb_mcp import vscode_bridge
from gdb_mcp.vscode_bridge import (
    BridgeClient,
    BridgeError,
    BridgeNotFound,
    VSCodeDebugger,
    discover,
)


@pytest.fixture
async def bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "bridges"))
    os.makedirs(vscode_bridge.BRIDGE_DIR, exist_ok=True)

    fake = FakeBridge()
    await fake.start()
    with open(
        os.path.join(vscode_bridge.BRIDGE_DIR, f"{os.getpid()}.json"), "w"
    ) as fh:
        json.dump(fake.discovery_info(), fh)
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
def debugger(bridge):
    return VSCodeDebugger()


# -- discovery ----------------------------------------------------------------


async def test_discover_finds_live_bridge(bridge):
    info = discover()
    assert info["port"] == bridge.port
    assert info["token"] == bridge.token


async def test_discover_without_bridge_explains_how_to_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "empty"))
    with pytest.raises(BridgeNotFound, match="GDB MCP Bridge extension"):
        discover()


async def test_discover_prunes_dead_windows(tmp_path, monkeypatch, bridge):
    """A VS Code window that died without cleaning up must not be selected."""
    stale = os.path.join(vscode_bridge.BRIDGE_DIR, "999999.json")
    with open(stale, "w") as fh:
        json.dump({"pid": 999999, "token": "x", "port": 1, "startedAt": 9e12}, fh)

    info = discover()
    assert info["port"] == bridge.port  # the live one, despite a newer timestamp
    assert not os.path.exists(stale)  # and the stale file is cleaned up


async def test_discover_prefers_matching_workspace(bridge, tmp_path, monkeypatch):
    other = os.path.join(vscode_bridge.BRIDGE_DIR, "other.json")
    with open(other, "w") as fh:
        json.dump(
            {
                "pid": os.getpid(),
                "token": "other",
                "port": 1,
                "startedAt": 9e12,
                "workspaceFolders": [str(tmp_path / "elsewhere")],
            },
            fh,
        )
    bridge.workspace_folders = [{"name": "ws", "path": str(tmp_path / "mine")}]
    with open(
        os.path.join(vscode_bridge.BRIDGE_DIR, f"{os.getpid()}.json"), "w"
    ) as fh:
        json.dump(bridge.discovery_info(), fh)

    info = discover(workspace=str(tmp_path / "mine" / "src"))
    assert info["token"] == bridge.token


async def test_bad_token_is_reported_clearly(bridge):
    info = bridge.discovery_info(token="wrong")
    client = BridgeClient(info)
    with pytest.raises(BridgeError, match="rejected the token"):
        await client.status()


async def test_unreachable_bridge_is_reported_clearly(bridge):
    info = bridge.discovery_info(port=1)
    client = BridgeClient(info)
    with pytest.raises(BridgeError, match="cannot reach the VS Code bridge"):
        await client.status()


# -- status -------------------------------------------------------------------


async def test_status_without_session_tells_the_agent_what_to_do(debugger):
    result = await debugger.status()
    assert result["state"] == "no active debug session"
    assert "vsc_launch" in result["hint"]


async def test_status_running(bridge, debugger):
    bridge.add_session()
    result = await debugger.status()
    assert result["state"] == "running"
    assert "vsc_pause" in result["hint"]


async def test_status_stopped_includes_frame_and_source(bridge, debugger, tmp_path):
    source = tmp_path / "main.c"
    source.write_text(
        "\n".join(f"line {n}" for n in range(1, 21)) + "\n", encoding="utf-8"
    )

    bridge.add_session()
    bridge.set_stopped(reason="breakpoint", thread_id=7)
    bridge.dap_responses["stackTrace"] = {
        "stackFrames": [
            {
                "id": 1000,
                "name": "parse_header",
                "source": {"path": str(source)},
                "line": 10,
            }
        ],
        "totalFrames": 1,
    }

    result = await debugger.status()
    assert result["state"] == "stopped"
    assert result["reason"] == "breakpoint"
    assert result["frame"]["name"] == "parse_header"
    assert result["frame"]["line"] == 10
    # source context is read locally and marks the current line
    current = [line for line in result["source"] if line["current"]]
    assert current[0]["line"] == 10


# -- the async-stop contract --------------------------------------------------


async def test_continue_waits_for_the_stopped_event(bridge, debugger):
    """DAP continue returns immediately; the stop arrives later as an event."""
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["continue"] = {"allThreadsContinued": True}
    bridge.dap_responses["stackTrace"] = {
        "stackFrames": [{"id": 1, "name": "worker", "source": {}, "line": 42}]
    }

    async def stop_later():
        await asyncio.sleep(0.1)
        bridge.set_stopped(reason="exception", thread_id=1)

    task = asyncio.create_task(stop_later())
    result = await debugger.resume("continue", timeout=5)
    await task

    assert result["state"] == "stopped"
    assert result["reason"] == "exception"
    assert result["frame"]["name"] == "worker"


async def test_continue_that_never_stops_reports_running(bridge, debugger):
    """It must not fabricate a stop when the program keeps running."""
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["continue"] = {}

    result = await debugger.resume("continue", timeout=0.5)
    assert result["state"] == "running"
    assert "vsc_pause" in result["note"]


async def test_program_exit_is_reported_as_exited(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["continue"] = {}

    async def exit_later():
        await asyncio.sleep(0.1)
        bridge.push_event("exited", exitCode=3)

    task = asyncio.create_task(exit_later())
    result = await debugger.resume("continue", timeout=5)
    await task

    assert result["state"] == "exited"
    assert result["exit_code"] == 3


async def test_stale_events_are_not_mistaken_for_a_new_stop(bridge, debugger):
    """A stop from before the resume must not satisfy the next wait."""
    bridge.add_session()
    bridge.set_stopped(reason="entry", thread_id=1)  # old event, seq=1
    bridge.dap_responses["continue"] = {}

    result = await debugger.resume("continue", timeout=0.5)
    assert result["state"] == "running"


async def test_step_maps_to_the_right_dap_request(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    for command in ("next", "stepIn", "stepOut"):
        bridge.dap_responses[command] = {}
    bridge.dap_responses["stackTrace"] = {"stackFrames": []}

    for kind, expected in (("next", "next"), ("step", "stepIn"), ("finish", "stepOut")):
        bridge.dap_calls.clear()
        await debugger.resume(kind, timeout=0.2)
        assert bridge.dap_calls[0][0] == expected


async def test_unknown_step_kind_is_rejected(debugger):
    with pytest.raises(ValueError, match="unknown kind"):
        await debugger.resume("teleport")


# -- inspection ---------------------------------------------------------------


async def test_backtrace_flattens_dap_frames(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=4)
    bridge.dap_responses["stackTrace"] = {
        "stackFrames": [
            {
                "id": 1,
                "name": "crash",
                "source": {"path": "/src/a.c"},
                "line": 12,
                "column": 3,
                "instructionPointerReference": "0x4011aa",
            },
            {"id": 2, "name": "main", "source": {"path": "/src/a.c"}, "line": 30},
        ],
        "totalFrames": 2,
    }

    result = await debugger.backtrace()
    assert result["thread_id"] == 4
    assert [f["name"] for f in result["frames"]] == ["crash", "main"]
    assert result["frames"][0]["pc"] == "0x4011aa"
    assert result["frames"][1]["file"] == "/src/a.c"


async def test_frame_expands_variables_one_level(bridge, debugger):
    bridge.add_session()
    bridge.dap_responses["scopes"] = {
        "scopes": [
            {"name": "Locals", "variablesReference": 10, "expensive": False},
            {"name": "Registers", "variablesReference": 20, "expensive": True},
        ]
    }

    def variables(args):
        if args["variablesReference"] == 10:
            return {
                "variables": [
                    {"name": "count", "value": "42", "type": "int"},
                    {
                        "name": "hdr",
                        "value": "{...}",
                        "type": "Header *",
                        "variablesReference": 11,
                        "evaluateName": "hdr",
                    },
                ]
            }
        if args["variablesReference"] == 11:
            return {"variables": [{"name": "len", "value": "8192", "type": "size_t"}]}
        return {"variables": []}

    bridge.dap_responses["variables"] = variables

    result = await debugger.frame(1000, expand_depth=1)
    locals_scope = result["scopes"][0]
    assert locals_scope["name"] == "Locals"
    assert locals_scope["variables"][0]["value"] == "42"
    assert locals_scope["variables"][1]["children"][0]["name"] == "len"
    # expensive scopes are skipped rather than silently fetched
    assert result["scopes"][1]["skipped"] == "marked expensive"


async def test_deep_structs_are_marked_expandable_not_recursed_forever(bridge, debugger):
    bridge.add_session()
    bridge.dap_responses["scopes"] = {
        "scopes": [{"name": "Locals", "variablesReference": 10, "expensive": False}]
    }
    bridge.dap_responses["variables"] = lambda args: {
        "variables": [
            {
                "name": "node",
                "value": "{...}",
                "variablesReference": 99,
                "evaluateName": "node->next",
            }
        ]
    }

    result = await debugger.frame(1, expand_depth=0)
    var = result["scopes"][0]["variables"][0]
    assert var["expandable"] is True
    assert var["expand_with"] == "node->next"


async def test_eval_uses_top_frame_when_none_given(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 777, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "0x0", "type": "char *"}

    result = await debugger.evaluate("buf")
    assert result["value"] == "0x0"
    assert result["type"] == "char *"
    evaluate_args = dict(bridge.dap_calls)["evaluate"]
    assert evaluate_args["frameId"] == 777


async def test_exec_gdb_uses_the_exec_escape(bridge, debugger):
    bridge.add_session()
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 5, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "rax 0x1"}

    result = await debugger.exec_gdb("info registers")
    args = dict(bridge.dap_calls)["evaluate"]
    assert args["expression"] == "-exec info registers"
    assert args["context"] == "repl"
    assert result["output"] == "rax 0x1"


async def test_program_output_is_drained(bridge, debugger):
    bridge.program_output = "segfault incoming\n"
    first = await debugger.program_output()
    second = await debugger.program_output()
    assert first["output"] == "segfault incoming\n"
    assert second["output"] == ""


# -- launch -------------------------------------------------------------------


async def test_launch_starts_config_and_waits_for_first_stop(bridge, debugger):
    bridge.dap_responses["stackTrace"] = {
        "stackFrames": [{"id": 1, "name": "main", "source": {}, "line": 5}]
    }

    async def stop_later():
        await asyncio.sleep(0.1)
        bridge.set_stopped(reason="breakpoint", thread_id=1)

    task = asyncio.create_task(stop_later())
    result = await debugger.launch("Debug my app", timeout=5)
    await task

    assert result["started"] is True
    assert result["session"]["name"] == "Debug my app"
    assert result["stop"]["state"] == "stopped"


async def test_launch_failure_surfaces_the_reason(bridge, debugger):
    bridge.launch_should_fail = True
    with pytest.raises(BridgeError, match="refused to start"):
        await debugger.launch("Broken config")


async def test_launch_can_skip_waiting(bridge, debugger):
    result = await debugger.launch("Debug my app", wait_for_stop=False)
    assert result["started"] is True
    assert "stop" not in result


async def test_configs_are_listed(bridge, debugger):
    bridge.configs = [{"name": "Debug my app", "type": "cppdbg", "request": "launch"}]
    result = await debugger.configs()
    assert result["workspaces"][0]["configurations"][0]["name"] == "Debug my app"


async def test_set_breakpoints_round_trips(bridge, debugger):
    await debugger.set_breakpoints([{"file": "/src/a.c", "line": 12}])
    listed = await debugger.list_breakpoints()
    assert listed["breakpoints"][0]["line"] == 12


# -- Unix socket transport (Linux/macOS only) ---------------------------------


@pytest.mark.skipif(
    not hasattr(asyncio, "open_unix_connection"),
    reason="Unix domain sockets are unavailable on this platform",
)
async def test_client_speaks_http_over_a_unix_socket(tmp_path, monkeypatch):
    """The transport actually used on Linux: HTTP over a Unix domain socket."""
    monkeypatch.setattr(vscode_bridge, "BRIDGE_DIR", str(tmp_path / "bridges"))
    os.makedirs(vscode_bridge.BRIDGE_DIR, exist_ok=True)

    fake = FakeBridge()
    # Keep the path short: sun_path is capped near 108 bytes.
    await fake.start_unix(str(tmp_path / "b.sock"))
    try:
        with open(
            os.path.join(vscode_bridge.BRIDGE_DIR, f"{os.getpid()}.json"), "w"
        ) as fh:
            json.dump(fake.discovery_info(), fh)

        info = discover()
        assert info["transport"] == "unix"
        assert "socketPath" in info and "port" not in info

        debugger = VSCodeDebugger()
        fake.add_session()
        fake.set_stopped(reason="breakpoint", thread_id=3)
        fake.dap_responses["stackTrace"] = {
            "stackFrames": [{"id": 1, "name": "parse_header", "source": {}, "line": 22}]
        }

        status = await debugger.status()
        assert status["state"] == "stopped"
        assert status["frame"]["name"] == "parse_header"

        bt = await debugger.backtrace()
        assert bt["frames"][0]["name"] == "parse_header"
    finally:
        await fake.stop()


# -- hex output parity with the gdb_* backend ---------------------------------


async def test_gdb_backed_session_is_switched_to_hex(bridge, debugger):
    """Otherwise the same variable reads 0x11 via gdb_eval and 17 via vsc_eval."""
    bridge.add_session(type="cppdbg")
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 1, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "0x11"}

    await debugger.evaluate("g_connection_count")
    sent = [args["expression"] for name, args in bridge.dap_calls if name == "evaluate"]
    assert "-exec set output-radix 16" in sent, sent


async def test_hex_is_configured_once_per_session(bridge, debugger):
    bridge.add_session(type="cppdbg")
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 1, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "0x11"}

    for _ in range(3):
        await debugger.evaluate("g_connection_count")
    sent = [args["expression"] for name, args in bridge.dap_calls if name == "evaluate"]
    assert sent.count("-exec set output-radix 16") == 1, sent


async def test_non_gdb_adapters_are_left_alone(bridge, debugger):
    """debugpy has no `-exec`; attempting it would just raise every call."""
    bridge.add_session(type="debugpy")
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 1, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "17"}

    await debugger.evaluate("g_connection_count")
    sent = [args["expression"] for name, args in bridge.dap_calls if name == "evaluate"]
    assert not any("output-radix" in expr for expr in sent), sent


async def test_hex_output_can_be_switched_off(bridge):
    from gdb_mcp.vscode_bridge import VSCodeDebugger

    plain = VSCodeDebugger(hex_output=False)
    bridge.add_session(type="cppdbg")
    bridge.set_stopped(thread_id=1)
    bridge.dap_responses["stackTrace"] = {"stackFrames": [{"id": 1, "name": "f"}]}
    bridge.dap_responses["evaluate"] = {"result": "17"}

    await plain.evaluate("g_connection_count")
    sent = [args["expression"] for name, args in bridge.dap_calls if name == "evaluate"]
    assert not any("output-radix" in expr for expr in sent), sent
