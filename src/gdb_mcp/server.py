"""MCP stdio server exposing the GDB and VS Code debugger tool surfaces.

This is a thin adapter: it declares the tool schemas and routes calls to
`gdb_mcp.tools.Debugger` (standalone GDB) and `gdb_mcp.vscode_bridge`
(the live VS Code session).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .session import GdbError, GdbStartupError
from .tools import Debugger
from .vscode_bridge import BridgeError, VSCodeDebugger

SESSION_PROP = {
    "type": "string",
    "default": "default",
    "description": "Session name. Omit unless you deliberately run several targets at once.",
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": {"session": SESSION_PROP, **properties},
        "required": required or [],
    }


TOOLS: list[Tool] = [
    Tool(
        name="gdb_start",
        description=(
            "Start a GDB session. Optionally load a binary at the same time. "
            "Call this before any other gdb_* tool."
        ),
        inputSchema=_schema(
            {
                "binary": {"type": "string", "description": "Path to the executable to debug."},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command-line arguments for the program.",
                },
                "cwd": {"type": "string", "description": "Working directory for GDB."},
                "use_init_file": {
                    "type": "boolean",
                    "default": False,
                    "description": "Load ~/.gdbinit (pretty printers etc). Off by default for reproducibility.",
                },
            }
        ),
    ),
    Tool(
        name="gdb_stop",
        description=(
            "End a GDB session. Defaults to detaching (leaving an attached "
            "process running); pass kill=true to terminate the inferior."
        ),
        inputSchema=_schema(
            {"kill": {"type": "boolean", "default": False, "description": "Kill the inferior instead of detaching."}}
        ),
    ),
    Tool(
        name="gdb_status",
        description="Where is the debuggee now: running or stopped, current frame, source context.",
        inputSchema=_schema({}),
    ),
    Tool(
        name="gdb_attach",
        description=(
            "Attach to a running process by PID. Opens a session if none is "
            "running. This STOPS the target process until you continue it, "
            "and requires ptrace permission."
        ),
        inputSchema=_schema(
            {
                "pid": {"type": "integer", "description": "PID of the process to attach to."},
                "binary": {"type": "string", "description": "Path to the binary, if symbols are not auto-found."},
            },
            ["pid"],
        ),
    ),
    Tool(
        name="gdb_load_core",
        description=(
            "Load a core dump for post-mortem analysis and return the crash "
            "backtrace. Opens a session if none is running."
        ),
        inputSchema=_schema(
            {
                "binary": {"type": "string", "description": "Executable that produced the core."},
                "core": {"type": "string", "description": "Path to the core file."},
            },
            ["binary", "core"],
        ),
    ),
    Tool(
        name="gdb_run",
        description="Run the loaded program from the start. Waits until it stops or exits.",
        inputSchema=_schema(
            {
                "args": {"type": "array", "items": {"type": "string"}},
                "stop_at_main": {
                    "type": "boolean",
                    "default": True,
                    "description": "Break at main immediately, so you can set breakpoints on lazily-loaded symbols.",
                },
                "timeout": {"type": "number", "default": 30, "description": "Seconds to wait for a stop."},
            }
        ),
    ),
    Tool(
        name="gdb_continue",
        description=(
            "Resume execution and wait for the next stop. If it returns "
            "state=running, the program did not stop within the timeout."
        ),
        inputSchema=_schema({"timeout": {"type": "number", "default": 30}}),
    ),
    Tool(
        name="gdb_step",
        description=(
            "Single-step. kind: step (into), next (over), finish (out of frame), "
            "stepi/nexti (instruction), until, return."
        ),
        inputSchema=_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["step", "next", "finish", "stepi", "nexti", "until", "return"],
                    "default": "step",
                },
                "count": {"type": "integer", "default": 1, "description": "Repeat this many times."},
                "timeout": {"type": "number", "default": 30},
            }
        ),
    ),
    Tool(
        name="gdb_interrupt",
        description="Halt a running inferior so you can inspect it.",
        inputSchema=_schema({}),
    ),
    Tool(
        name="gdb_break",
        description="Set a breakpoint at a function, file:line, or *address.",
        inputSchema=_schema(
            {
                "location": {"type": "string", "description": "e.g. 'main', 'foo.c:42', '*0x400ab0'"},
                "condition": {"type": "string", "description": "Only stop when this expression is true."},
                "temporary": {"type": "boolean", "default": False, "description": "Delete after first hit."},
                "ignore_count": {"type": "integer", "description": "Skip the first N hits."},
            },
            ["location"],
        ),
    ),
    Tool(
        name="gdb_watch",
        description="Set a watchpoint that stops when an expression's value changes.",
        inputSchema=_schema(
            {
                "expression": {"type": "string"},
                "kind": {"type": "string", "enum": ["write", "read", "access"], "default": "write"},
            },
            ["expression"],
        ),
    ),
    Tool(
        name="gdb_breakpoints",
        description="List all breakpoints and watchpoints with their hit counts.",
        inputSchema=_schema({}),
    ),
    Tool(
        name="gdb_delete_breakpoint",
        description="Delete a breakpoint by number.",
        inputSchema=_schema({"number": {"type": "integer"}}, ["number"]),
    ),
    Tool(
        name="gdb_backtrace",
        description="Show the call stack of the current (or given) thread.",
        inputSchema=_schema(
            {
                "limit": {"type": "integer", "default": 30},
                "full": {"type": "boolean", "default": False, "description": "Include local variables."},
                "thread": {"type": "integer", "description": "Thread id; defaults to current."},
            }
        ),
    ),
    Tool(
        name="gdb_frame",
        description="Select a stack frame and return its locals, arguments, and source context.",
        inputSchema=_schema({"level": {"type": "integer", "description": "0 is the innermost frame."}}, ["level"]),
    ),
    Tool(
        name="gdb_eval",
        description=(
            "Evaluate a C/C++ expression in the selected frame. Inferior "
            "function calls are blocked unless allow_function_calls=true, "
            "because they really execute inside the target process."
        ),
        inputSchema=_schema(
            {
                "expression": {"type": "string"},
                "allow_function_calls": {"type": "boolean", "default": False},
            },
            ["expression"],
        ),
    ),
    Tool(
        name="gdb_threads",
        description="List all threads with their current frames.",
        inputSchema=_schema({}),
    ),
    Tool(
        name="gdb_select_thread",
        description="Switch the current thread.",
        inputSchema=_schema({"thread_id": {"type": "integer"}}, ["thread_id"]),
    ),
    Tool(
        name="gdb_registers",
        description="Read CPU registers, optionally filtered by name.",
        inputSchema=_schema({"names": {"type": "array", "items": {"type": "string"}}}),
    ),
    Tool(
        name="gdb_memory",
        description=(
            "Dump a range of memory in hex. Reads `count` words of `word_size` "
            "bytes, laid out in rows with an ASCII gutter. word_size follows "
            "GDB's x/ sizes: 4 = word, 8 = giant word (pointers on x86-64)."
        ),
        inputSchema=_schema(
            {
                "address": {
                    "type": "string",
                    "description": "Any GDB expression: '0x7fff...', '&buf', '$sp', 'pkt->payload'",
                },
                "count": {"type": "integer", "default": 16, "description": "Number of words to read."},
                "word_size": {
                    "type": "integer",
                    "enum": [4, 8],
                    "default": 8,
                    "description": "Bytes per word: 4 = word, 8 = giant word.",
                },
                "per_row": {"type": "integer", "description": "Words per row; defaults to a 16-byte row."},
            },
            ["address"],
        ),
    ),
    Tool(
        name="gdb_globals",
        description=(
            "List global and file-static variables, optionally with their "
            "current values. Locals tools do NOT show globals -- use this to "
            "discover them. Pass a `pattern` regex to narrow the results."
        ),
        inputSchema=_schema(
            {
                "pattern": {
                    "type": "string",
                    "description": "Regex matched against the name. Strongly recommended.",
                },
                "include_values": {
                    "type": "boolean",
                    "default": False,
                    "description": "Evaluate each variable to fetch its current value.",
                },
                "limit": {"type": "integer", "default": 200},
            }
        ),
    ),
    Tool(
        name="gdb_disassemble",
        description=(
            "Disassemble around the current PC or a given location. Returns a "
            "flat list of instructions, each annotated with its source line."
        ),
        inputSchema=_schema(
            {
                "location": {"type": "string"},
                "count": {"type": "integer", "default": 20},
            }
        ),
    ),
    Tool(
        name="gdb_source",
        description="Show source code at the current location or a given file:line.",
        inputSchema=_schema(
            {"location": {"type": "string"}, "lines": {"type": "integer", "default": 20}}
        ),
    ),
    Tool(
        name="gdb_program_output",
        description="Drain anything the debugged program has written to its stdout/stderr.",
        inputSchema=_schema({}),
    ),
    Tool(
        name="gdb_raw",
        description=(
            "Escape hatch: run an arbitrary GDB CLI command and return its "
            "console output. Use when no dedicated tool fits."
        ),
        inputSchema=_schema({"command": {"type": "string"}}, ["command"]),
    ),
]


def _vsc(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


#: Tools that drive the debug session VS Code already owns, via the bridge
#: extension.  Use these whenever the user launched with F5 -- a second gdb
#: cannot attach to a process the VS Code adapter is already tracing.
VSCODE_TOOLS: list[Tool] = [
    Tool(
        name="vsc_status",
        description=(
            "PREFERRED FIRST CALL when the user is debugging in VS Code. Reports "
            "whether a debug session is active, whether it is stopped, and the "
            "current frame with surrounding source."
        ),
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_configs",
        description="List the launch.json configurations available in the workspace.",
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_launch",
        description=(
            "Start a launch.json configuration -- exactly what pressing F5 does, "
            "including its preLaunchTask, envFile and args. Use this to run the "
            "project after making changes."
        ),
        inputSchema=_vsc(
            {
                "name": {"type": "string", "description": "Configuration name from launch.json."},
                "folder": {"type": "string", "description": "Workspace folder name, for multi-root."},
                "wait_for_stop": {
                    "type": "boolean",
                    "default": True,
                    "description": "Wait for the first breakpoint/exception before returning.",
                },
                "timeout": {"type": "number", "default": 120},
            },
            ["name"],
        ),
    ),
    Tool(
        name="vsc_terminate",
        description="Stop the active VS Code debug session.",
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_continue",
        description=(
            "Resume the program and wait for the next stop. Returns state=running "
            "if it did not stop within the timeout."
        ),
        inputSchema=_vsc({"timeout": {"type": "number", "default": 30}}),
    ),
    Tool(
        name="vsc_step",
        description="Step the program: next (over), step (into), stepOut (finish frame).",
        inputSchema=_vsc(
            {
                "kind": {
                    "type": "string",
                    "enum": ["next", "step", "stepIn", "stepOut", "finish"],
                    "default": "next",
                },
                "thread_id": {"type": "integer"},
                "timeout": {"type": "number", "default": 30},
            }
        ),
    ),
    Tool(
        name="vsc_pause",
        description="Halt a running program so you can inspect it.",
        inputSchema=_vsc({"timeout": {"type": "number", "default": 15}}),
    ),
    Tool(
        name="vsc_wait_stop",
        description=(
            "Block until the program hits a breakpoint, crashes, or exits. Use "
            "after launching a program that runs for a while."
        ),
        inputSchema=_vsc({"timeout": {"type": "number", "default": 60}}),
    ),
    Tool(
        name="vsc_threads",
        description="List threads in the VS Code debug session.",
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_backtrace",
        description="Call stack of the stopped thread, with frame ids for vsc_frame.",
        inputSchema=_vsc(
            {
                "thread_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 30},
            }
        ),
    ),
    Tool(
        name="vsc_frame",
        description=(
            "Locals, arguments and registers for a frame, with structs expanded "
            "one level. Pass a frame id from vsc_backtrace."
        ),
        inputSchema=_vsc(
            {
                "frame_id": {"type": "integer"},
                "expand_depth": {"type": "integer", "default": 1},
            },
            ["frame_id"],
        ),
    ),
    Tool(
        name="vsc_eval",
        description="Evaluate a C/C++ expression in a frame of the VS Code session.",
        inputSchema=_vsc(
            {
                "expression": {"type": "string"},
                "frame_id": {"type": "integer", "description": "Defaults to the top frame."},
            },
            ["expression"],
        ),
    ),
    Tool(
        name="vsc_exec",
        description=(
            "Run a raw GDB command inside the VS Code session via cppdbg's "
            "'-exec' escape (e.g. 'info registers', 'thread apply all bt'). "
            "Requires a GDB-backed session: not available with cppvsdbg (the "
            "Windows Visual Studio debugger), debugpy, or delve."
        ),
        inputSchema=_vsc({"command": {"type": "string"}}, ["command"]),
    ),
    Tool(
        name="vsc_breakpoints",
        description="List the breakpoints currently set in VS Code.",
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_set_breakpoints",
        description=(
            "Add or remove breakpoints in VS Code's UI, so the user sees them in "
            "the editor. Each entry is {file, line} or {functionName}, with an "
            "optional condition."
        ),
        inputSchema=_vsc(
            {
                "breakpoints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                            "functionName": {"type": "string"},
                            "condition": {"type": "string"},
                            "hitCondition": {"type": "string"},
                        },
                    },
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "clear"],
                    "default": "add",
                },
            },
            ["breakpoints"],
        ),
    ),
    Tool(
        name="vsc_output",
        description=(
            "Drain stdout/stderr the debugged program has produced. Output is "
            "delivered once: stop and step results already include whatever "
            "was pending, so an empty result here means nothing new."
        ),
        inputSchema=_vsc({}),
    ),
    Tool(
        name="vsc_disassemble",
        description="Disassemble at an address reference from vsc_backtrace's `pc`.",
        inputSchema=_vsc(
            {
                "memory_reference": {"type": "string"},
                "count": {"type": "integer", "default": 20},
            },
            ["memory_reference"],
        ),
    ),
    Tool(
        name="vsc_memory",
        description=(
            "Dump a range of memory in hex, in rows with an ASCII gutter. "
            "Accepts a raw address or an expression like '&buf' or "
            "'pkt->payload'. word_size: 4 = word, 8 = giant word."
        ),
        inputSchema=_vsc(
            {
                "memory_reference": {
                    "type": "string",
                    "description": "Hex address, a `pc` from vsc_backtrace, or an expression.",
                },
                "count": {"type": "integer", "default": 16, "description": "Number of words to read."},
                "word_size": {
                    "type": "integer",
                    "enum": [4, 8],
                    "default": 8,
                    "description": "Bytes per word: 4 = word, 8 = giant word.",
                },
                "per_row": {"type": "integer", "description": "Words per row; defaults to a 16-byte row."},
            },
            ["memory_reference"],
        ),
    ),
    Tool(
        name="vsc_globals",
        description=(
            "List global and file-static variables. Needed for cppdbg, whose "
            "frames expose only Locals and Registers. Adapters that publish a "
            "Globals scope (debugpy) already surface them through vsc_frame, so "
            "check there first. Requires a GDB-backed session."
        ),
        inputSchema=_vsc(
            {
                "pattern": {"type": "string", "description": "Regex matched against the name."},
                "include_values": {"type": "boolean", "default": False},
            }
        ),
    ),
]

ALL_TOOLS = VSCODE_TOOLS + TOOLS


def select_tools(which: str = "all") -> list[Tool]:
    """Trim the exposed surface.

    42 tools is a lot for a model to choose between. If you only ever debug
    through VS Code, set GDB_MCP_TOOLS=vscode and the standalone GDB tools
    disappear (and vice versa for headless core-dump work).
    """
    which = (which or "all").strip().lower()
    if which in ("vscode", "vsc"):
        return list(VSCODE_TOOLS)
    if which in ("gdb", "standalone"):
        return list(TOOLS)
    return list(ALL_TOOLS)


def build_server(
    debugger: Debugger | None = None, vscode: VSCodeDebugger | None = None
) -> Server:
    dbg = debugger or Debugger(gdb_path=os.environ.get("GDB_PATH", "gdb"))
    vsc = vscode or VSCodeDebugger(workspace=os.environ.get("GDB_MCP_WORKSPACE"))
    exposed = select_tools(os.environ.get("GDB_MCP_TOOLS", "all"))
    server: Server = Server("gdb-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return exposed

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        args = dict(arguments or {})
        failed = True
        try:
            if name.startswith("vsc_"):
                result = await _dispatch_vscode(vsc, name, args)
            else:
                session = args.pop("session", "default")
                result = await _dispatch(dbg, name, session, args)
            failed = False
        except BridgeError as exc:
            result = {"error": str(exc), "tool": name}
        except (GdbError, GdbStartupError, ValueError) as exc:
            result = {"error": str(exc), "tool": name}
        except TypeError as exc:
            result = {"error": f"bad arguments for {name}: {exc}", "tool": name}
        except asyncio.TimeoutError as exc:
            result = {
                "error": f"timed out: {exc}",
                "tool": name,
                "hint": "The program may still be running; try vsc_pause or gdb_interrupt.",
            }
        # isError must be set, or a client that checks it reads a failed
        # debugger call -- no gdb, no bridge, bad breakpoint -- as success.
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(result, indent=2, default=str))
            ],
            isError=failed,
        )

    return server


async def _dispatch_vscode(
    vsc: VSCodeDebugger, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    match name:
        case "vsc_status":
            return await vsc.status()
        case "vsc_configs":
            return await vsc.configs()
        case "vsc_launch":
            return await vsc.launch(**args)
        case "vsc_terminate":
            return await vsc.terminate()
        case "vsc_continue":
            return await vsc.resume("continue", **args)
        case "vsc_step":
            kind = args.pop("kind", "next")
            return await vsc.resume(kind, **args)
        case "vsc_pause":
            return await vsc.resume("pause", **args)
        case "vsc_wait_stop":
            return await vsc.wait_stop(**args)
        case "vsc_threads":
            return await vsc.threads()
        case "vsc_backtrace":
            return await vsc.backtrace(**args)
        case "vsc_frame":
            return await vsc.frame(**args)
        case "vsc_eval":
            return await vsc.evaluate(**args)
        case "vsc_exec":
            return await vsc.exec_gdb(**args)
        case "vsc_breakpoints":
            return await vsc.list_breakpoints()
        case "vsc_set_breakpoints":
            return await vsc.set_breakpoints(**args)
        case "vsc_output":
            return await vsc.program_output()
        case "vsc_disassemble":
            return await vsc.disassemble(**args)
        case "vsc_memory":
            return await vsc.read_memory(**args)
        case "vsc_globals":
            return await vsc.list_globals(**args)
        case _:
            raise ValueError(f"Unknown tool: {name}")


async def _dispatch(
    dbg: Debugger, name: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    match name:
        case "gdb_start":
            return await dbg.start(session=session, **args)
        case "gdb_stop":
            return await dbg.stop(session=session, **args)
        case "gdb_status":
            return await dbg.status(session=session)
        case "gdb_attach":
            return await dbg.attach(session=session, **args)
        case "gdb_load_core":
            return await dbg.load_core(session=session, **args)
        case "gdb_run":
            return await dbg.run(session=session, **args)
        case "gdb_continue":
            return await dbg.cont(session=session, **args)
        case "gdb_step":
            return await dbg.step(session=session, **args)
        case "gdb_interrupt":
            return await dbg.interrupt(session=session)
        case "gdb_break":
            return await dbg.set_breakpoint(session=session, **args)
        case "gdb_watch":
            return await dbg.set_watchpoint(session=session, **args)
        case "gdb_breakpoints":
            return await dbg.list_breakpoints(session=session)
        case "gdb_delete_breakpoint":
            return await dbg.delete_breakpoint(session=session, **args)
        case "gdb_backtrace":
            return await dbg.backtrace(session=session, **args)
        case "gdb_frame":
            return await dbg.select_frame(session=session, **args)
        case "gdb_eval":
            return await dbg.evaluate(session=session, **args)
        case "gdb_threads":
            return await dbg.list_threads(session=session)
        case "gdb_select_thread":
            return await dbg.select_thread(session=session, **args)
        case "gdb_registers":
            return await dbg.registers(session=session, **args)
        case "gdb_memory":
            return await dbg.read_memory(session=session, **args)
        case "gdb_globals":
            return await dbg.list_globals(session=session, **args)
        case "gdb_disassemble":
            return await dbg.disassemble(session=session, **args)
        case "gdb_source":
            return await dbg.list_source(session=session, **args)
        case "gdb_program_output":
            return await dbg.program_output(session=session)
        case "gdb_raw":
            return await dbg.raw(session=session, **args)
        case _:
            raise ValueError(f"Unknown tool: {name}")


async def amain() -> None:
    dbg = Debugger(gdb_path=os.environ.get("GDB_PATH", "gdb"))
    server = build_server(dbg)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    finally:
        await dbg.shutdown_all()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
