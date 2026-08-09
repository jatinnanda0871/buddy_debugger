"""MCP stdio server exposing the GDB and VS Code debugger tool surfaces.

This is a thin adapter: it declares the tool schemas and routes calls to
`gdb_mcp.tools.Debugger` (standalone GDB) and `gdb_mcp.vscode_bridge`
(the live VS Code session).

The surface is 7 dispatcher tools (4 `gdb_*`, 3 `vsc_*`), not one tool per
operation: each takes an `operation` enum plus that operation's own fields.
`allOf`/`if`/`then` branches on `operation` still enforce per-operation
`required` fields the same way separate tools would -- see `_dispatch_tool`.
Grouped by what the operations *do* (session lifecycle, execution control,
breakpoint management, read-only inspection), not forced together just to
hit a lower number: those four are different enough in shape and intent that
cramming them into fewer tools would make both the code and the model's job
harder, not easier.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .session import GdbError, GdbStartupError
from .tools import Debugger
from .vscode_bridge import BridgeError, VSCodeDebugger

# -- configuration (environment variables this server reads) ------------------

ENV_GDB_PATH = "GDB_PATH"  #: gdb binary path/name. Default: "gdb" (must be on PATH).
ENV_WORKSPACE = "GDB_MCP_WORKSPACE"  #: Prefer the vsc bridge whose workspace matches this path. Default: none.
ENV_VSCODE_HEX = "GDB_MCP_VSCODE_HEX"  #: "0" leaves vsc_* sessions in decimal. Default: hex, matching gdb_*.
ENV_TOOLS = "GDB_MCP_TOOLS"  #: "gdb" | "vscode" | "all" -- which tool families to expose. Default: "all".

# -- schema-dispatch machinery --------------------------------------------------

SESSION_PROP = {
    "type": "string",
    "default": "default",
    "description": "Session name. Omit unless you deliberately run several targets at once.",
}


def _dispatch_tool(
    name: str,
    description: str,
    operations: dict[str, dict[str, Any]],
    *,
    with_session: bool = True,
) -> Tool:
    """Build one MCP tool that dispatches on an `operation` enum.

    `operations` maps operation name -> {"description", "properties",
    "required"}. Every operation's fields are declared once in the merged
    top-level `properties` (so nothing is repeated per branch); `allOf` with
    one `if`/`then` per operation is what still enforces that operation's
    `required` fields, exactly as a separate per-operation tool would.
    """
    properties: dict[str, Any] = {"session": SESSION_PROP} if with_session else {}
    properties["operation"] = {
        "type": "string",
        "enum": list(operations),
        "description": "\n".join(
            f"{op}: {spec['description']}" for op, spec in operations.items()
        ),
    }
    for spec in operations.values():
        properties.update(spec.get("properties", {}))

    all_of = [
        {
            "if": {"properties": {"operation": {"const": op}}},
            "then": {"required": ["operation", *spec.get("required", [])]},
        }
        for op, spec in operations.items()
    ]

    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": ["operation"],
            "allOf": all_of,
        },
    )


def _pop_operation(name: str, args: dict[str, Any]) -> str:
    """Pull `operation` out of the call args.

    The schema's own `required: ["operation"]` should always catch a missing
    one before this runs; this is only a defensive backstop against a client
    that skips validation, so it fails as a clean ValueError rather than a
    raw KeyError leaking dispatch internals.
    """
    operation = args.pop("operation", None)
    if not operation:
        raise ValueError(f"{name}: 'operation' is required")
    return operation


# ==============================================================================
# gdb_* -- the standalone GDB backend
# ==============================================================================

# -- gdb_session ----------------------------------------------------------------

GDB_SESSION_OPS: dict[str, dict[str, Any]] = {
    "start": {
        "description": "Start a GDB session, optionally loading a binary. Call this before any other gdb_* operation.",
        "properties": {
            "binary": {
                "type": "string",
                "description": (
                    "Path to an executable. Meaning depends on operation: the "
                    "program to run (start), extra symbol source (attach), or "
                    "the program that produced the core (load_core)."
                ),
            },
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
        },
        "required": [],
    },
    "stop": {
        "description": "End the session. Detaches by default, leaving an attached process running; pass kill=true to terminate the inferior.",
        "properties": {
            "kill": {
                "type": "boolean",
                "default": False,
                "description": "Kill the inferior instead of detaching.",
            },
        },
        "required": [],
    },
    "status": {
        "description": "Where is the debuggee now: running or stopped, current frame, source context.",
        "properties": {},
        "required": [],
    },
    "attach": {
        "description": "Attach to a running process by PID. Opens a session if none is running. STOPS the target process until you continue it, and requires ptrace permission.",
        "properties": {
            "pid": {"type": "integer", "description": "PID of the process to attach to."},
        },
        "required": ["pid"],
    },
    "load_core": {
        "description": "Load a core dump for post-mortem analysis and return the crash backtrace. Opens a session if none is running.",
        "properties": {
            "core": {"type": "string", "description": "Path to the core file."},
        },
        "required": ["binary", "core"],
    },
    "record": {
        "description": (
            "Start or stop recording execution history (process record-and-replay), "
            "required before gdb_exec(operation='reverse-*') will work. Real overhead: "
            "turn it on only when needed, and only after stepping past any call into "
            "code with no line info (e.g. strlen) -- gdb_exec can hang for minutes "
            "single-stepping through one of those while recording is active."
        ),
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"], "default": "start"},
            "method": {
                "type": "string",
                "description": "Recording method, e.g. 'full' (default) or 'btrace'. Omit for GDB's default.",
            },
        },
        "required": [],
    },
}


async def _dispatch_gdb_session(
    dbg: Debugger, operation: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    match operation:
        case "start":
            return await dbg.start(session=session, **args)
        case "stop":
            return await dbg.stop(session=session, **args)
        case "status":
            return await dbg.status(session=session)
        case "attach":
            return await dbg.attach(session=session, **args)
        case "load_core":
            return await dbg.load_core(session=session, **args)
        case "record":
            return await dbg.record(session=session, **args)
        case _:
            raise ValueError(f"Unknown gdb_session operation: {operation!r}")


# -- gdb_exec ---------------------------------------------------------------------

#: Forwarded straight to Debugger.step()'s STEP_KINDS.
_GDB_STEP_KINDS = ("step", "next", "finish", "stepi", "nexti", "until", "return")
#: ...and to Debugger.reverse()'s REVERSE_KINDS, prefixed "reverse-".
_GDB_REVERSE_KINDS = ("continue", "step", "next", "finish")

GDB_EXEC_OPS: dict[str, dict[str, Any]] = {
    "run": {
        "description": "Run the loaded program from the start. Waits until it stops or exits.",
        "properties": {
            "args": {"type": "array", "items": {"type": "string"}},
            "stop_at_main": {
                "type": "boolean",
                "default": True,
                "description": "Break at main immediately, so you can set breakpoints on lazily-loaded symbols.",
            },
            "timeout": {"type": "number", "default": 30, "description": "Seconds to wait for a stop."},
        },
        "required": [],
    },
    "continue": {
        "description": "Resume execution and wait for the next stop. If it returns state=running, the program did not stop within the timeout.",
        "properties": {},
        "required": [],
    },
    "interrupt": {
        "description": "Halt a running inferior so you can inspect it.",
        "properties": {},
        "required": [],
    },
    **{
        kind: {
            "description": {
                "step": "Single-step into calls.",
                "next": "Single-step over calls.",
                "finish": "Run until the current frame returns.",
                "stepi": "Step one machine instruction, into calls.",
                "nexti": "Step one machine instruction, over calls.",
                "until": "Run until a line greater than the current one is reached (escapes a loop).",
                "return": "Force the current frame to return immediately.",
            }[kind],
            "properties": {
                "count": {"type": "integer", "default": 1, "description": "Repeat this many times."},
                "timeout": {"type": "number", "default": 30},
            },
            "required": [],
        }
        for kind in _GDB_STEP_KINDS
    },
    **{
        f"reverse-{kind}": {
            "description": (
                f"Run backward: {'to the previous stop' if kind == 'continue' else kind + ', backward'}. "
                "Undo execution to see how a bad value got there, instead of reproducing the bug from "
                "the start. Requires gdb_session(operation='record', action='start') first. Same result "
                "shape as the forward operations, including the locals diff -- 'new' is where execution "
                "is heading (backward), 'old' is where it just was."
            ),
            "properties": {
                "count": {"type": "integer", "default": 1, "description": "Repeat this many times."},
                "timeout": {"type": "number", "default": 30},
            },
            "required": [],
        }
        for kind in _GDB_REVERSE_KINDS
    },
}


async def _dispatch_gdb_exec(
    dbg: Debugger, operation: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    if operation == "run":
        return await dbg.run(session=session, **args)
    if operation == "continue":
        return await dbg.cont(session=session, **args)
    if operation == "interrupt":
        return await dbg.interrupt(session=session)
    if operation in _GDB_STEP_KINDS:
        return await dbg.step(operation, session=session, **args)
    if operation.startswith("reverse-"):
        return await dbg.reverse(operation[len("reverse-") :], session=session, **args)
    raise ValueError(f"Unknown gdb_exec operation: {operation!r}")


# -- gdb_breakpoint -----------------------------------------------------------------

GDB_BREAKPOINT_OPS: dict[str, dict[str, Any]] = {
    "set": {
        "description": "Create a breakpoint at a function, file:line, or *address.",
        "properties": {
            "location": {"type": "string", "description": "e.g. 'main', 'foo.c:42', '*0x400ab0'"},
            "condition": {"type": "string", "description": "Only stop when this expression is true."},
            "temporary": {"type": "boolean", "default": False, "description": "Delete after first hit."},
            "ignore_count": {"type": "integer", "description": "Skip this many hits before stopping."},
        },
        "required": ["location"],
    },
    "watch": {
        "description": "Create a watchpoint that stops when an expression's value changes.",
        "properties": {
            "expression": {"type": "string"},
            "kind": {"type": "string", "enum": ["write", "read", "access"], "default": "write"},
        },
        "required": ["expression"],
    },
    "list": {
        "description": "List all breakpoints and watchpoints with their hit counts.",
        "properties": {},
        "required": [],
    },
    "delete": {
        "description": "Delete a breakpoint by number.",
        "properties": {"number": {"type": "integer"}},
        "required": ["number"],
    },
    "enable": {
        "description": "Enable a breakpoint by number, without deleting it.",
        "properties": {"number": {"type": "integer"}},
        "required": ["number"],
    },
    "disable": {
        "description": "Disable a breakpoint by number, without deleting it.",
        "properties": {"number": {"type": "integer"}},
        "required": ["number"],
    },
    "modify": {
        "description": (
            "Change an existing breakpoint's condition or ignore count in place, "
            "without deleting and recreating it (which would lose its number and "
            "accumulated hit count). condition=\"\" clears the condition; "
            "ignore_count=0 clears the ignore count."
        ),
        "properties": {
            "number": {"type": "integer"},
            "condition": {"type": "string", "description": "Only stop when this expression is true."},
            "ignore_count": {"type": "integer", "description": "Skip this many hits before stopping."},
        },
        "required": ["number"],
    },
}


async def _dispatch_gdb_breakpoint(
    dbg: Debugger, operation: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    match operation:
        case "set":
            return await dbg.set_breakpoint(session=session, **args)
        case "watch":
            return await dbg.set_watchpoint(session=session, **args)
        case "list":
            return await dbg.list_breakpoints(session=session)
        case "delete":
            return await dbg.delete_breakpoint(session=session, **args)
        case "enable":
            return await dbg.enable_breakpoint(session=session, enabled=True, **args)
        case "disable":
            return await dbg.enable_breakpoint(session=session, enabled=False, **args)
        case "modify":
            return await dbg.modify_breakpoint(session=session, **args)
        case _:
            raise ValueError(f"Unknown gdb_breakpoint operation: {operation!r}")


# -- gdb_inspect --------------------------------------------------------------------

GDB_INSPECT_OPS: dict[str, dict[str, Any]] = {
    "backtrace": {
        "description": "Call stack of the current (or given) thread.",
        "properties": {
            "limit": {"type": "integer", "default": 30},
            "full": {"type": "boolean", "default": False, "description": "Include local variables."},
            "thread_id": {"type": "integer", "description": "Thread id; defaults to current."},
        },
        "required": [],
    },
    "frame": {
        "description": "Select a stack frame and return its locals, arguments, and source context.",
        "properties": {"level": {"type": "integer", "description": "0 is the innermost frame."}},
        "required": ["level"],
    },
    "eval": {
        "description": (
            "Evaluate a C/C++ expression in the selected frame. Inferior function "
            "calls are blocked unless allow_function_calls=true, because they "
            "really execute inside the target process."
        ),
        "properties": {
            "expression": {"type": "string"},
            "allow_function_calls": {"type": "boolean", "default": False},
        },
        "required": ["expression"],
    },
    "threads": {
        "description": "List all threads with their current frames.",
        "properties": {},
        "required": [],
    },
    "select_thread": {
        "description": "Switch the current thread.",
        "properties": {"thread_id": {"type": "integer"}},
        "required": ["thread_id"],
    },
    "registers": {
        "description": "Read CPU registers, optionally filtered by name.",
        "properties": {"names": {"type": "array", "items": {"type": "string"}}},
        "required": [],
    },
    "memory": {
        "description": (
            "Dump a range of memory in hex. Reads `count` words of `word_size` "
            "bytes, laid out in rows with an ASCII gutter. word_size follows "
            "GDB's x/ sizes: 4 = word, 8 = giant word (pointers on x86-64)."
        ),
        "properties": {
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
        "required": ["address"],
    },
    "globals": {
        "description": (
            "List global and file-static variables, optionally with their current "
            "values. Locals-based operations do NOT show globals -- use this to "
            "discover them. Pass a `pattern` regex to narrow the results."
        ),
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "Unanchored regex matched against the name. Anchor it ('^g_') "
                    "or it also matches mid-name and drags in linked-library symbols."
                ),
            },
            "include_values": {"type": "boolean", "default": False},
            "max_results": {"type": "integer", "default": 200},
        },
        "required": [],
    },
    "disassemble": {
        "description": "Disassemble around the current PC or a given location. Returns a flat list of instructions, each annotated with its source line.",
        "properties": {
            "location": {"type": "string"},
            "instruction_count": {"type": "integer", "default": 20},
        },
        "required": [],
    },
    "source": {
        "description": "Show source code at the current location or a given file:line.",
        "properties": {
            "location": {"type": "string"},
            "lines": {"type": "integer", "default": 20},
        },
        "required": [],
    },
    "program_output": {
        "description": "Drain anything the debugged program has written to its stdout/stderr.",
        "properties": {},
        "required": [],
    },
    "raw": {
        "description": "Escape hatch: run an arbitrary GDB CLI command and return its console output. Use when no dedicated operation fits.",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


async def _dispatch_gdb_inspect(
    dbg: Debugger, operation: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    match operation:
        case "backtrace":
            if "thread_id" in args:
                args["thread"] = args.pop("thread_id")
            return await dbg.backtrace(session=session, **args)
        case "frame":
            return await dbg.select_frame(session=session, **args)
        case "eval":
            return await dbg.evaluate(session=session, **args)
        case "threads":
            return await dbg.list_threads(session=session)
        case "select_thread":
            return await dbg.select_thread(session=session, **args)
        case "registers":
            return await dbg.registers(session=session, **args)
        case "memory":
            return await dbg.read_memory(session=session, **args)
        case "globals":
            if "max_results" in args:
                args["limit"] = args.pop("max_results")
            return await dbg.list_globals(session=session, **args)
        case "disassemble":
            if "instruction_count" in args:
                args["count"] = args.pop("instruction_count")
            return await dbg.disassemble(session=session, **args)
        case "source":
            return await dbg.list_source(session=session, **args)
        case "program_output":
            return await dbg.program_output(session=session)
        case "raw":
            return await dbg.raw(session=session, **args)
        case _:
            raise ValueError(f"Unknown gdb_inspect operation: {operation!r}")


TOOLS: list[Tool] = [
    _dispatch_tool(
        "gdb_session",
        "Manage a standalone GDB session's lifecycle: start it, stop it, check its status, attach to a running process, load a core dump, or toggle execution recording.",
        GDB_SESSION_OPS,
    ),
    _dispatch_tool(
        "gdb_exec",
        "Advance or rewind execution and wait for the next stop. Forward operations: run, continue, interrupt, step, next, finish, stepi, nexti, until, return. Backward (requires gdb_session record first): reverse-continue, reverse-step, reverse-next, reverse-finish.",
        GDB_EXEC_OPS,
    ),
    _dispatch_tool(
        "gdb_breakpoint",
        "Create, list, or edit breakpoints and watchpoints.",
        GDB_BREAKPOINT_OPS,
    ),
    _dispatch_tool(
        "gdb_inspect",
        "Read-only: stack, locals, expressions, threads, registers, memory, globals, disassembly, source, captured program output, plus a raw-GDB-command escape hatch.",
        GDB_INSPECT_OPS,
    ),
]


async def _dispatch_gdb(
    dbg: Debugger, name: str, session: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Route a gdb_* tool call to its operation's handler."""
    operation = _pop_operation(name, args)
    try:
        match name:
            case "gdb_session":
                return await _dispatch_gdb_session(dbg, operation, session, args)
            case "gdb_exec":
                return await _dispatch_gdb_exec(dbg, operation, session, args)
            case "gdb_breakpoint":
                return await _dispatch_gdb_breakpoint(dbg, operation, session, args)
            case "gdb_inspect":
                return await _dispatch_gdb_inspect(dbg, operation, session, args)
            case _:
                raise ValueError(f"Unknown tool: {name}")
    except TypeError as exc:
        raise TypeError(f"operation={operation!r}: {exc}") from exc


# ==============================================================================
# vsc_* -- the VS Code bridge backend
# ==============================================================================

# -- vsc_session ------------------------------------------------------------------

VSC_SESSION_OPS: dict[str, dict[str, Any]] = {
    "status": {
        "description": (
            "PREFERRED FIRST CALL when the user is debugging in VS Code. Reports "
            "whether a debug session is active, whether it is stopped, and the "
            "current frame with surrounding source."
        ),
        "properties": {},
        "required": [],
    },
    "configs": {
        "description": "List the launch.json configurations available in the workspace.",
        "properties": {},
        "required": [],
    },
    "launch": {
        "description": (
            "Start a launch.json configuration -- exactly what pressing F5 does, "
            "including its preLaunchTask, envFile and args. Use this to run the "
            "project after making changes."
        ),
        "properties": {
            "name": {"type": "string", "description": "Configuration name from launch.json."},
            "folder": {"type": "string", "description": "Workspace folder name, for multi-root."},
            "wait_for_stop": {
                "type": "boolean",
                "default": True,
                "description": "Wait for the first breakpoint/exception before returning.",
            },
            "timeout": {"type": "number", "default": 120},
        },
        "required": ["name"],
    },
    "terminate": {
        "description": "Stop the active VS Code debug session.",
        "properties": {},
        "required": [],
    },
}


async def _dispatch_vsc_session(
    vsc: VSCodeDebugger, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    match operation:
        case "status":
            return await vsc.status()
        case "configs":
            return await vsc.configs()
        case "launch":
            return await vsc.launch(**args)
        case "terminate":
            return await vsc.terminate()
        case _:
            raise ValueError(f"Unknown vsc_session operation: {operation!r}")


# -- vsc_exec -----------------------------------------------------------------------

#: Forwarded straight to VSCodeDebugger.resume(), which validates the kind itself.
_VSC_RESUME_OPS = ("continue", "next", "step", "stepIn", "stepOut", "finish", "pause")

VSC_EXEC_OPS: dict[str, dict[str, Any]] = {
    **{
        op: {
            "description": {
                "continue": "Resume the program and wait for the next stop.",
                "next": "Step over (the DAP 'next' request).",
                "step": "Step into (the DAP 'stepIn' request).",
                "stepIn": "Step into.",
                "stepOut": "Step out of the current frame (finish).",
                "finish": "Step out of the current frame (same as stepOut).",
                "pause": "Halt a running program so you can inspect it.",
            }[op],
            "properties": {
                "thread_id": {"type": "integer"},
                "timeout": {"type": "number", "default": 30},
            },
            "required": [],
        }
        for op in _VSC_RESUME_OPS
    },
    "wait_stop": {
        "description": "Block until the program hits a breakpoint, crashes, or exits. Use after launching a program that runs for a while.",
        "properties": {"timeout": {"type": "number", "default": 60}},
        "required": [],
    },
}


async def _dispatch_vsc_exec(
    vsc: VSCodeDebugger, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    if operation == "wait_stop":
        return await vsc.wait_stop(**args)
    if operation in _VSC_RESUME_OPS:
        return await vsc.resume(operation, **args)
    raise ValueError(f"Unknown vsc_exec operation: {operation!r}")


# -- vsc_inspect --------------------------------------------------------------------

VSC_INSPECT_OPS: dict[str, dict[str, Any]] = {
    "threads": {
        "description": "List threads in the VS Code debug session.",
        "properties": {},
        "required": [],
    },
    "backtrace": {
        "description": "Call stack of the stopped thread, with frame ids for operation=frame.",
        "properties": {
            "thread_id": {"type": "integer"},
            "limit": {"type": "integer", "default": 30},
        },
        "required": [],
    },
    "frame": {
        "description": "Locals, arguments and registers for a frame, with structs expanded one level. Pass a frame id from operation=backtrace.",
        "properties": {
            "frame_id": {"type": "integer"},
            "expand_depth": {"type": "integer", "default": 1},
        },
        "required": ["frame_id"],
    },
    "eval": {
        "description": "Evaluate a C/C++ expression in a frame of the VS Code session.",
        "properties": {
            "expression": {"type": "string"},
            "frame_id": {"type": "integer", "description": "Defaults to the top frame."},
        },
        "required": ["expression"],
    },
    "breakpoints": {
        "description": "List the breakpoints currently set in VS Code.",
        "properties": {},
        "required": [],
    },
    "set_breakpoints": {
        "description": "Add or remove breakpoints in VS Code's UI, so the user sees them in the editor. Each entry is {file, line} or {functionName}, with an optional condition.",
        "properties": {
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
            "action": {"type": "string", "enum": ["add", "remove", "clear"], "default": "add"},
        },
        "required": ["breakpoints"],
    },
    "output": {
        "description": (
            "Drain stdout/stderr the debugged program has produced. Output is "
            "delivered once: stop and step results already include whatever was "
            "pending, so an empty result here means nothing new."
        ),
        "properties": {},
        "required": [],
    },
    "disassemble": {
        "description": "Disassemble at an address reference from operation=backtrace's `pc`.",
        "properties": {
            "memory_reference": {"type": "string"},
            "instruction_count": {"type": "integer", "default": 20},
        },
        "required": ["memory_reference"],
    },
    "memory": {
        "description": (
            "Dump a range of memory in hex, in rows with an ASCII gutter. Accepts "
            "a raw address or an expression like '&buf' or 'pkt->payload'. "
            "word_size: 4 = word, 8 = giant word."
        ),
        "properties": {
            "memory_reference": {
                "type": "string",
                "description": "Hex address, a `pc` from operation=backtrace, or an expression.",
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
        "required": ["memory_reference"],
    },
    "globals": {
        "description": (
            "List global and file-static variables. Needed for cppdbg, whose "
            "frames expose only Locals and Registers. Adapters that publish a "
            "Globals scope (debugpy) already surface them through operation=frame, "
            "so check there first. Requires a GDB-backed session."
        ),
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Unanchored regex matched against the name; anchor it ('^g_') to exclude linked-library symbols.",
            },
            "include_values": {"type": "boolean", "default": False},
        },
        "required": [],
    },
    "raw": {
        "description": (
            "Escape hatch: run a raw GDB command inside the VS Code session via "
            "cppdbg's '-exec' escape (e.g. 'info registers', 'thread apply all "
            "bt'). Requires a GDB-backed session: not available with cppvsdbg "
            "(the Windows Visual Studio debugger), debugpy, or delve."
        ),
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


async def _dispatch_vsc_inspect(
    vsc: VSCodeDebugger, operation: str, args: dict[str, Any]
) -> dict[str, Any]:
    match operation:
        case "threads":
            return await vsc.threads()
        case "backtrace":
            return await vsc.backtrace(**args)
        case "frame":
            return await vsc.frame(**args)
        case "eval":
            return await vsc.evaluate(**args)
        case "breakpoints":
            return await vsc.list_breakpoints()
        case "set_breakpoints":
            return await vsc.set_breakpoints(**args)
        case "output":
            return await vsc.program_output()
        case "disassemble":
            if "instruction_count" in args:
                args["count"] = args.pop("instruction_count")
            return await vsc.disassemble(**args)
        case "memory":
            return await vsc.read_memory(**args)
        case "globals":
            return await vsc.list_globals(**args)
        case "raw":
            return await vsc.exec_gdb(**args, tool_name="vsc_inspect(operation='raw')")
        case _:
            raise ValueError(f"Unknown vsc_inspect operation: {operation!r}")


VSCODE_TOOLS: list[Tool] = [
    _dispatch_tool(
        "vsc_session",
        "Manage the VS Code debug session VS Code already owns: check status, list launch.json configs, launch one (same as pressing F5), or terminate.",
        VSC_SESSION_OPS,
        with_session=False,
    ),
    _dispatch_tool(
        "vsc_exec",
        "Resume, step, pause, or wait for the VS Code debug session to stop.",
        VSC_EXEC_OPS,
        with_session=False,
    ),
    _dispatch_tool(
        "vsc_inspect",
        "Read-only: threads, stack, frame locals, expressions, breakpoints, captured output, disassembly, memory, globals, plus a raw-GDB-command escape hatch.",
        VSC_INSPECT_OPS,
        with_session=False,
    ),
]


async def _dispatch_vsc(
    vsc: VSCodeDebugger, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Route a vsc_* tool call to its operation's handler."""
    operation = _pop_operation(name, args)
    try:
        match name:
            case "vsc_session":
                return await _dispatch_vsc_session(vsc, operation, args)
            case "vsc_exec":
                return await _dispatch_vsc_exec(vsc, operation, args)
            case "vsc_inspect":
                return await _dispatch_vsc_inspect(vsc, operation, args)
            case _:
                raise ValueError(f"Unknown tool: {name}")
    except TypeError as exc:
        raise TypeError(f"operation={operation!r}: {exc}") from exc


# ==============================================================================
# server assembly
# ==============================================================================

ALL_TOOLS = VSCODE_TOOLS + TOOLS


def select_tools(which: str = "all") -> list[Tool]:
    """Trim the exposed surface.

    If you only ever debug through VS Code, set GDB_MCP_TOOLS=vscode and the
    standalone GDB tools disappear (and vice versa for headless core-dump
    work).
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
    dbg = debugger or Debugger(gdb_path=os.environ.get(ENV_GDB_PATH, "gdb"))
    vsc = vscode or VSCodeDebugger(
        workspace=os.environ.get(ENV_WORKSPACE),
        hex_output=os.environ.get(ENV_VSCODE_HEX, "1") != "0",
    )
    exposed = select_tools(os.environ.get(ENV_TOOLS, "all"))
    server: Server = Server("gdb-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return exposed

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        args = dict(arguments or {})
        try:
            if name.startswith("vsc_"):
                result = await _dispatch_vsc(vsc, name, args)
            else:
                session = args.pop("session", "default")
                result = await _dispatch_gdb(dbg, name, session, args)
        except BridgeError as exc:
            error = {"error": str(exc), "tool": name}
        except (GdbError, GdbStartupError, ValueError) as exc:
            error = {"error": str(exc), "tool": name}
        except TypeError as exc:
            error = {"error": f"bad arguments for {name}: {exc}", "tool": name}
        except asyncio.TimeoutError as exc:
            error = {
                "error": f"timed out: {exc}",
                "tool": name,
                "hint": "The program may still be running; try vsc_exec(operation='pause') or gdb_exec(operation='interrupt').",
            }
        else:
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
        # Raise instead of building a CallToolResult ourselves: the mcp SDK's
        # own except-handler turns this into isError=True. Constructing
        # CallToolResult directly only works on mcp>=1.11 -- older SDKs
        # (e.g. 1.10.x) treat a returned CallToolResult as a plain iterable
        # of its pydantic fields instead of a result object, corrupting the
        # response. Raising works identically across SDK versions.
        raise RuntimeError(json.dumps(error, indent=2, default=str))

    return server


# ==============================================================================
# entrypoint
# ==============================================================================


async def amain() -> None:
    dbg = Debugger(gdb_path=os.environ.get(ENV_GDB_PATH, "gdb"))
    server = build_server(dbg)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    finally:
        await dbg.shutdown_all()


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten a (Base)ExceptionGroup down to its non-group leaves.

    anyio's task groups wrap concurrent failures -- e.g. a broken stdio pipe
    racing a Ctrl-C -- in an ExceptionGroup/BaseExceptionGroup rather than
    raising a single exception, so a plain `except KeyboardInterrupt` can
    silently miss one that arrives bundled with something else.
    """
    exceptions = getattr(exc, "exceptions", None)
    if exceptions is None:
        return [exc]
    leaves: list[BaseException] = []
    for sub in exceptions:
        leaves.extend(_leaf_exceptions(sub))
    return leaves


def _is_clean_shutdown(exc: BaseException) -> bool:
    """True if every leaf of `exc` is just a Ctrl-C -- nothing worth logging."""
    return all(isinstance(leaf, KeyboardInterrupt) for leaf in _leaf_exceptions(exc))


def main() -> None:
    try:
        asyncio.run(amain())
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        if _is_clean_shutdown(exc):
            return
        # The client tore down the stdio pipe (killed/reconnected the
        # subprocess, usually after its own call timeout fired) while a
        # tool call was still in flight; the delayed response write then
        # fails as a broken pipe (anyio.BrokenResourceError and friends,
        # often wrapped in an (Base)ExceptionGroup by anyio's task groups).
        # The transport is dead either way, so there's nothing left to do
        # but log clearly instead of an unhandled traceback and exit --
        # the client is expected to relaunch the server for its next call.
        print("gdb-mcp: transport closed while a tool call was still running:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
