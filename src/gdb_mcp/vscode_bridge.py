"""Client for the VS Code GDB MCP Bridge extension.

Talks DAP to the debug session VS Code already owns.  This is the only way to
inspect a session started with F5: Linux allows one ptrace tracer per process,
so once cppdbg is attached, a second gdb cannot join it.

Transport is HTTP over a Unix domain socket (loopback TCP on Windows), with a
bearer token.  Stdlib only -- a small HTTP/1.1 client using
`Connection: close`, which keeps framing trivial and long polls honest.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import glob
import json
import os
import re
import time
from typing import Any

from .tools import (
    WORD_SIZES,
    build_word_rows,
    render_rows,
    source_context,
    truncate,
)

#: `info variables` lines look like: "42:\tstatic int g_count;"
_INFO_VARIABLE_RE = re.compile(r"^\s*(?:(\d+):\s*)?(.+?;)\s*$")
_DECL_NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])*\s*;")


def _parse_info_variables(text: str) -> list[dict[str, Any]]:
    """Pull names and declarations out of GDB's `info variables` output."""
    found: list[dict[str, Any]] = []
    current_file: str | None = None
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("File "):
            current_file = stripped[5:].rstrip(":")
            continue
        if stripped.startswith(("All variables", "Non-debugging symbols")):
            current_file = None
            continue
        match = _INFO_VARIABLE_RE.match(line)
        if not match:
            continue
        declaration = match.group(2)
        name_match = _DECL_NAME_RE.search(declaration)
        if not name_match:
            continue
        name = name_match.group(1)
        if name in seen:
            continue
        seen.add(name)
        entry: dict[str, Any] = {"name": name, "declaration": declaration}
        if current_file:
            entry["file"] = current_file
        if match.group(1):
            entry["line"] = int(match.group(1))
        found.append(entry)
    return found

BRIDGE_DIR = os.path.join(os.path.expanduser("~"), ".gdb-mcp", "bridges")

#: DAP request per step kind.
RESUME_COMMANDS = {
    "continue": "continue",
    "next": "next",
    "step": "stepIn",
    "stepIn": "stepIn",
    "stepOut": "stepOut",
    "finish": "stepOut",
    "pause": "pause",
}

STOP_EVENTS = ("stopped", "terminated", "exited")

#: How long to let trailing `output` events land after the program exits.
TRAILING_OUTPUT_GRACE = 0.35

#: Adapter types whose REPL understands GDB's `-exec` escape. Everything else
#: (cppvsdbg on Windows, debugpy, delve, ...) is DAP-only.
GDB_BACKED_TYPES = {"cppdbg"}


class BridgeError(RuntimeError):
    """The bridge refused a request, or is unreachable."""


class BridgeNotFound(BridgeError):
    """No live VS Code window is running the bridge extension."""


def _pid_alive_windows(pid: int) -> bool:
    """Liveness through the Win32 API.

    Deliberately *not* os.kill(pid, 0): on Windows CPython routes non-console
    signals to TerminateProcess, and whether signal 0 is special-cased has
    varied between versions. Since the PIDs here are VS Code windows, guessing
    wrong would kill the user's editor. OpenProcess is unambiguous everywhere.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    STILL_ACTIVE = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            # Access denied means it exists but belongs to another user.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):
        # If we cannot tell, assume alive: a stale entry merely fails to
        # connect, whereas wrongly pruning a live one loses the session.
        return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def discover(workspace: str | None = None) -> dict[str, Any]:
    """Find a live bridge, preferring one whose workspace matches `workspace`."""
    candidates: list[dict[str, Any]] = []
    for path in glob.glob(os.path.join(BRIDGE_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue
        pid = int(info.get("pid", 0))
        if not _pid_alive(pid):
            # VS Code went away without cleaning up.
            with contextlib.suppress(OSError):
                os.unlink(path)
            continue
        info["_file"] = path
        candidates.append(info)

    if not candidates:
        raise BridgeNotFound(
            "No VS Code window is running the GDB MCP Bridge extension. "
            "Open the project in VS Code and check that the extension is "
            "installed and enabled (command: 'GDB MCP Bridge: Show Status')."
        )

    if workspace:
        target = os.path.abspath(workspace)
        for info in candidates:
            folders = [os.path.abspath(f) for f in info.get("workspaceFolders", [])]
            if any(target == f or target.startswith(f + os.sep) for f in folders):
                return info

    candidates.sort(key=lambda i: i.get("startedAt", 0), reverse=True)
    return candidates[0]


class BridgeClient:
    """Minimal async HTTP client for one bridge endpoint."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.token = info["token"]
        self.socket_path = info.get("socketPath")
        self.host = info.get("host", "127.0.0.1")
        self.port = info.get("port")
        self.workspace_folders = info.get("workspaceFolders", [])

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.socket_path:
            if not hasattr(asyncio, "open_unix_connection"):
                raise BridgeError("Unix sockets are unavailable on this platform")
            return await asyncio.open_unix_connection(self.socket_path)
        return await asyncio.open_connection(self.host, self.port)

    async def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(
                self._call(method, path, body), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise BridgeError(f"bridge did not answer {path} within {timeout}s") from exc
        except (ConnectionError, FileNotFoundError, OSError) as exc:
            raise BridgeError(
                f"cannot reach the VS Code bridge ({exc}). Is that window still open?"
            ) from exc

    async def _call(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        reader, writer = await self._open()
        try:
            payload = b"" if body is None else json.dumps(body).encode("utf-8")
            lines = [
                f"{method} {path} HTTP/1.1",
                "Host: localhost",
                f"Authorization: Bearer {self.token}",
                "Connection: close",
                "Accept: application/json",
            ]
            if body is not None:
                lines.append("Content-Type: application/json")
                lines.append(f"Content-Length: {len(payload)}")
            head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
            writer.write(head + payload)
            await writer.drain()

            raw = await reader.read()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

        head_sep = raw.find(b"\r\n\r\n")
        if head_sep < 0:
            raise BridgeError("malformed response from bridge")
        header_text = raw[:head_sep].decode("utf-8", "replace")
        content = raw[head_sep + 4 :]

        status_line = header_text.splitlines()[0] if header_text else ""
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        try:
            parsed = json.loads(content.decode("utf-8", "replace") or "{}")
        except ValueError as exc:
            raise BridgeError(f"bridge returned non-JSON ({status})") from exc

        if status == 401:
            raise BridgeError("bridge rejected the token; restart VS Code's bridge")
        if status >= 400 or not parsed.get("ok", False):
            raise BridgeError(parsed.get("error") or f"bridge error (HTTP {status})")
        return parsed

    # -- convenience --------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        return await self.call("GET", "/status")

    async def dap(
        self,
        command: str,
        args: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        payload: dict[str, Any] = {"command": command, "args": args or {}}
        if session_id:
            payload["sessionId"] = session_id
        result = await self.call("POST", "/request", payload, timeout=timeout)
        return result.get("body")

    async def events(self, since: int, wait: float) -> dict[str, Any]:
        wait_s = int(max(0, min(wait, 60)))
        return await self.call(
            "GET",
            f"/events?since={int(since)}&wait={wait_s}",
            timeout=wait_s + 15,
        )


class VSCodeDebugger:
    """Tool surface over a live VS Code debug session."""

    def __init__(
        self, workspace: str | None = None, *, hex_output: bool = True
    ) -> None:
        self.workspace = workspace
        #: Match the gdb_* backend's `set output-radix 16`, so the same
        #: variable does not read 0x11 through one backend and 17 through the
        #: other. This is the user's own session, so it also changes what the
        #: Variables pane and hover tooltips show -- hence the opt-out.
        self.hex_output = hex_output
        self._hex_configured: set[str] = set()
        self._client: BridgeClient | None = None

    def client(self, *, refresh: bool = False) -> BridgeClient:
        if self._client is None or refresh:
            self._client = BridgeClient(discover(self.workspace))
        return self._client

    async def _dap(self, command: str, args: dict[str, Any] | None = None, **kw) -> Any:
        try:
            return await self.client().dap(command, args, **kw)
        except BridgeError as exc:
            # A debug adapter that is shutting down rejects in-flight requests
            # with a bare "Canceled", which tells the caller nothing. This is
            # the common case of inspecting state after the program exited.
            lowered = str(exc).lower()
            if "cancel" not in lowered and "no debugger" not in lowered:
                raise
            with contextlib.suppress(BridgeError):
                if await self._active_session() is None:
                    raise BridgeError(
                        f"the debug session ended before {command!r} could run, "
                        "so there is no state left to inspect. Check vsc_status, "
                        "or start a new session with vsc_launch."
                    ) from exc
            raise BridgeError(
                f"the debug adapter canceled {command!r} -- the session is "
                "shutting down. Check vsc_status."
            ) from exc

    # -- session ------------------------------------------------------------

    async def status(self, *, with_frame: bool = True) -> dict[str, Any]:
        raw = await self.client(refresh=True).status()
        active_id = raw.get("activeSessionId")
        active = next(
            (s for s in raw.get("sessions", []) if s.get("id") == active_id), None
        )
        out: dict[str, Any] = {
            "bridge": "connected",
            "workspace_folders": raw.get("workspaceFolders", []),
            "last_seq": raw.get("lastSeq"),
            "session_count": len(raw.get("sessions", [])),
        }
        if active is None:
            out["state"] = "no active debug session"
            out["hint"] = (
                "Start one with F5, or call vsc_launch with a launch.json "
                "configuration name (vsc_configs lists them)."
            )
            return out

        out["session"] = {
            "id": active.get("id"),
            "name": active.get("name"),
            "type": active.get("type"),
        }
        if not active.get("stopped"):
            out["state"] = "running"
            out["hint"] = "Call vsc_pause to halt it, or vsc_wait_stop."
            return out

        out["state"] = "stopped"
        last_stop = active.get("lastStop") or {}
        out["reason"] = last_stop.get("reason")
        for key in ("description", "text"):
            if last_stop.get(key):
                out[key] = last_stop[key]
        thread_id = last_stop.get("threadId") or active.get("threadId")
        if with_frame and thread_id is not None:
            out.update(await self._frame_summary(thread_id))
        return out

    async def configs(self) -> dict[str, Any]:
        return await self.client(refresh=True).call("GET", "/configs")

    async def launch(
        self,
        name: str | None = None,
        *,
        folder: str | None = None,
        config: dict[str, Any] | None = None,
        wait_for_stop: bool = True,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Start a launch.json configuration -- the same path as pressing F5,
        including its preLaunchTask and envFile."""
        client = self.client(refresh=True)
        before = (await client.status()).get("lastSeq", 0)
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if config:
            body["config"] = config
        if folder:
            body["folder"] = folder
        result = await client.call("POST", "/launch", body, timeout=timeout)

        out: dict[str, Any] = {
            "started": True,
            "session": result.get("session"),
        }
        if not wait_for_stop:
            return out
        event, _seq = await self._await_stop(before, timeout)
        if event is not None and event.get("type") == "stopped":
            with contextlib.suppress(BridgeError):
                await self._ensure_hex_output()
        out["stop"] = await self._describe_event(event)
        return out

    async def terminate(self) -> dict[str, Any]:
        return await self.client().call("POST", "/stop", {})

    # -- execution ----------------------------------------------------------

    async def resume(
        self,
        kind: str = "continue",
        *,
        thread_id: int | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if kind not in RESUME_COMMANDS:
            raise ValueError(
                f"unknown kind {kind!r}; use one of: {', '.join(sorted(RESUME_COMMANDS))}"
            )
        client = self.client()
        before = (await client.status()).get("lastSeq", 0)
        if thread_id is None:
            thread_id = await self._current_thread_id()
        await self._dap(RESUME_COMMANDS[kind], {"threadId": thread_id})
        event, _seq = await self._await_stop(before, timeout)
        return await self._describe_event(event, running_hint=True)

    async def wait_stop(self, *, timeout: float = 60.0) -> dict[str, Any]:
        client = self.client()
        before = (await client.status()).get("lastSeq", 0)
        event, _seq = await self._await_stop(before, timeout)
        return await self._describe_event(event, running_hint=True)

    async def _await_stop(
        self, since: int, timeout: float
    ) -> tuple[dict[str, Any] | None, int]:
        """Poll the event stream until the program actually stops.

        DAP `continue` returns immediately; the `stopped` event arrives later.
        Returning on the request's own response would hand back a stale view.
        """
        deadline = time.monotonic() + timeout
        cursor = since
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, cursor
            batch = await self.client().events(cursor, min(remaining, 20))
            for event in batch.get("events", []):
                cursor = max(cursor, int(event.get("seq", cursor)))
                if event.get("type") in STOP_EVENTS:
                    return event, cursor
            cursor = max(cursor, int(batch.get("lastSeq", cursor)))

    async def _fold_in_output(self, out: dict[str, Any]) -> None:
        """Attach pending program output; never let it cost us the report."""
        with contextlib.suppress(BridgeError):
            output = await self.program_output()
            if output.get("output"):
                out["program_output"] = output["output"]

    async def _describe_event(
        self, event: dict[str, Any] | None, *, running_hint: bool = False
    ) -> dict[str, Any]:
        if event is None:
            out: dict[str, Any] = {"state": "running"}
            if running_hint:
                out["note"] = (
                    "The program did not stop within the timeout. It is still "
                    "running -- call vsc_pause to halt it, or vsc_wait_stop "
                    "with a longer timeout."
                )
            return out

        kind = event.get("type")
        if kind in ("exited", "terminated"):
            out = {"state": kind}
            if kind == "exited":
                out["exit_code"] = event.get("exitCode")
            else:
                out["note"] = "The debug session ended."
            # The adapter emits trailing `output` events just after the exit
            # event, so draining immediately loses the program's last words --
            # exactly what you want when it died.
            await asyncio.sleep(TRAILING_OUTPUT_GRACE)
            await self._fold_in_output(out)
            return out

        out = {"state": "stopped", "reason": event.get("reason")}
        for key in ("description", "text", "hitBreakpointIds"):
            if event.get(key):
                out[key] = event[key]
        thread_id = event.get("threadId")
        if thread_id is not None:
            out["thread_id"] = thread_id
            out.update(await self._frame_summary(thread_id))
        await self._fold_in_output(out)
        return out

    # -- inspection ---------------------------------------------------------

    async def _current_thread_id(self) -> int:
        raw = await self.client().status()
        active_id = raw.get("activeSessionId")
        active = next(
            (s for s in raw.get("sessions", []) if s.get("id") == active_id), None
        )
        if active:
            last_stop = active.get("lastStop") or {}
            for value in (last_stop.get("threadId"), active.get("threadId")):
                if isinstance(value, int):
                    return value
        threads = await self._dap("threads")
        listing = (threads or {}).get("threads") or []
        if not listing:
            raise BridgeError("no threads reported by the debug adapter")
        return listing[0]["id"]

    async def _frame_summary(self, thread_id: int) -> dict[str, Any]:
        """Top frame plus source context -- the 'where am I' answer."""
        try:
            trace = await self._dap(
                "stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 1}
            )
        except BridgeError:
            return {}
        frames = (trace or {}).get("stackFrames") or []
        if not frames:
            return {}
        top = frames[0]
        source = (top.get("source") or {}).get("path")
        out: dict[str, Any] = {
            "frame": {
                "id": top.get("id"),
                "name": top.get("name"),
                "file": source,
                "line": top.get("line"),
            }
        }
        ctx = await asyncio.to_thread(source_context, source, top.get("line"))
        if ctx:
            out["source"] = ctx
        return out

    async def threads(self) -> dict[str, Any]:
        result = await self._dap("threads")
        return {"threads": (result or {}).get("threads", [])}

    async def backtrace(
        self, *, thread_id: int | None = None, limit: int = 30
    ) -> dict[str, Any]:
        if thread_id is None:
            thread_id = await self._current_thread_id()
        result = await self._dap(
            "stackTrace",
            {"threadId": thread_id, "startFrame": 0, "levels": int(limit)},
        )
        frames = []
        for frame in (result or {}).get("stackFrames", []):
            frames.append(
                {
                    "id": frame.get("id"),
                    "name": frame.get("name"),
                    "file": (frame.get("source") or {}).get("path"),
                    "line": frame.get("line"),
                    "column": frame.get("column"),
                    "pc": frame.get("instructionPointerReference"),
                }
            )
        return {
            "thread_id": thread_id,
            "frames": frames,
            "total_frames": (result or {}).get("totalFrames", len(frames)),
        }

    async def frame(
        self, frame_id: int, *, expand_depth: int = 1, max_children: int = 60
    ) -> dict[str, Any]:
        """Scopes and variables for a frame, with children expanded one level."""
        await self._ensure_hex_output()
        scopes_result = await self._dap("scopes", {"frameId": int(frame_id)})
        out: dict[str, Any] = {"frame_id": frame_id, "scopes": []}
        for scope in (scopes_result or {}).get("scopes", []):
            if scope.get("expensive"):
                out["scopes"].append(
                    {"name": scope.get("name"), "skipped": "marked expensive"}
                )
                continue
            variables = await self._variables(
                scope.get("variablesReference", 0),
                depth=expand_depth,
                max_children=max_children,
            )
            out["scopes"].append({"name": scope.get("name"), "variables": variables})
        return out

    async def _variables(
        self, reference: int, *, depth: int, max_children: int
    ) -> list[dict[str, Any]]:
        if not reference or depth < 0:
            return []
        result = await self._dap("variables", {"variablesReference": reference})
        out: list[dict[str, Any]] = []
        for var in (result or {}).get("variables", [])[:max_children]:
            entry: dict[str, Any] = {
                "name": var.get("name"),
                "value": truncate(str(var.get("value", "")), 2000),
            }
            if var.get("type"):
                entry["type"] = var["type"]
            child_ref = var.get("variablesReference") or 0
            if child_ref and depth > 0:
                children = await self._variables(
                    child_ref, depth=depth - 1, max_children=max_children
                )
                if children:
                    entry["children"] = children
            elif child_ref:
                entry["expandable"] = True
                if var.get("evaluateName"):
                    entry["expand_with"] = var["evaluateName"]
            out.append(entry)
        return out

    async def evaluate(
        self,
        expression: str,
        *,
        frame_id: int | None = None,
        context: str = "watch",
    ) -> dict[str, Any]:
        await self._ensure_hex_output()
        if frame_id is None:
            frame_id = await self._top_frame_id()
        args: dict[str, Any] = {"expression": expression, "context": context}
        if frame_id is not None:
            args["frameId"] = frame_id
        result = await self._dap("evaluate", args)
        out: dict[str, Any] = {
            "expression": expression,
            "value": truncate(str((result or {}).get("result", "")), 8000),
            "type": (result or {}).get("type"),
        }
        # cppdbg renders an aggregate as a bare summary -- a char[32] evaluates
        # to "[32]" -- and puts the contents in children. Without expanding,
        # asking for an array or struct returns nothing usable.
        reference = (result or {}).get("variablesReference") or 0
        if reference:
            children = await self._variables(reference, depth=0, max_children=64)
            if children:
                out["children"] = children
        return out

    async def _active_session(self) -> dict[str, Any] | None:
        raw = await self.client().status()
        active_id = raw.get("activeSessionId")
        return next(
            (s for s in raw.get("sessions", []) if s.get("id") == active_id), None
        )

    async def _require_gdb_backend(self, tool: str) -> None:
        """Fail clearly when the adapter has no GDB underneath.

        On Windows, a C/C++ launch config commonly uses `cppvsdbg` (the Visual
        Studio debugger), which has no `-exec` escape at all. Without this the
        user just gets a confusing empty result.
        """
        session = await self._active_session()
        kind = (session or {}).get("type")
        if kind is None or kind in GDB_BACKED_TYPES:
            return
        detail = (
            "the Visual Studio debugger, which has no GDB underneath"
            if kind == "cppvsdbg"
            else f"a '{kind}' adapter"
        )
        raise BridgeError(
            f"{tool} needs a GDB-backed debug session, but this one is {detail}. "
            "Use vsc_eval / vsc_frame / vsc_backtrace, which are plain DAP and "
            "work with any adapter. For GDB features on Windows, set the "
            "launch.json type to 'cppdbg' with a MinGW or WSL gdb."
        )

    async def _ensure_hex_output(self) -> None:
        """Put a GDB-backed session into hex, once, before reading values.

        Applied lazily rather than on connect because scenario one is the user
        pressing F5 themselves -- there is no launch call to hook.
        """
        if not self.hex_output:
            return
        session = await self._active_session()
        if session is None:
            return
        session_id = session.get("id")
        if not session_id or session_id in self._hex_configured:
            return
        # Record first: a non-GDB adapter can never be configured, and a
        # failure should not be retried on every single evaluation.
        self._hex_configured.add(session_id)
        if session.get("type") not in GDB_BACKED_TYPES:
            return
        with contextlib.suppress(BridgeError):
            await self.exec_gdb("set output-radix 16", tool_name="vsc_eval")

    async def exec_gdb(
        self, command: str, *, tool_name: str = "vsc_exec"
    ) -> dict[str, Any]:
        """Run a raw GDB command through cppdbg's `-exec` REPL escape."""
        await self._require_gdb_backend(tool_name)
        frame_id = await self._top_frame_id()
        args: dict[str, Any] = {
            "expression": f"-exec {command}",
            "context": "repl",
        }
        if frame_id is not None:
            args["frameId"] = frame_id
        result = await self._dap("evaluate", args, timeout=60)
        text = str((result or {}).get("result", "") or "")
        # cppdbg often streams -exec output to the Debug Console rather than
        # returning it, so fold in anything that showed up there.
        return {"command": command, "output": truncate(text, 12000)}

    async def _top_frame_id(self) -> int | None:
        try:
            thread_id = await self._current_thread_id()
            trace = await self._dap(
                "stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 1}
            )
        except BridgeError:
            return None
        frames = (trace or {}).get("stackFrames") or []
        return frames[0].get("id") if frames else None

    async def set_breakpoints(
        self, breakpoints: list[dict[str, Any]], *, action: str = "add"
    ) -> dict[str, Any]:
        return await self.client().call(
            "POST", "/breakpoints", {"action": action, "breakpoints": breakpoints}
        )

    async def list_breakpoints(self) -> dict[str, Any]:
        return await self.client().call("GET", "/breakpoints")

    async def program_output(self) -> dict[str, Any]:
        result = await self.client().call("GET", "/output")
        text = truncate(result.get("output", ""), 8000)
        out: dict[str, Any] = {"output": text}
        if not text:
            # Reads drain the buffer, and every stop report folds pending
            # output into itself. Empty here means "nothing new", which is not
            # the same as "the program printed nothing".
            out["note"] = (
                "No output since it was last reported. Output is delivered "
                "once: anything the program printed earlier was included in "
                "the preceding stop or step result."
            )
        return out

    async def disassemble(
        self, memory_reference: str, *, count: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        result = await self._dap(
            "disassemble",
            {
                "memoryReference": memory_reference,
                "instructionCount": int(count),
                "instructionOffset": int(offset),
                "resolveSymbols": True,
            },
        )
        return {"instructions": (result or {}).get("instructions", [])}

    async def _resolve_address(self, expression: str) -> str:
        """Turn an expression into a memoryReference DAP will accept.

        DAP wants an opaque memory reference, but an agent naturally reaches for
        `&buf` or `packet->payload`, so evaluate anything that is not already
        a bare address.
        """
        text = expression.strip()
        if text.startswith("0x") or text.isdigit():
            return text
        frame_id = await self._top_frame_id()
        args: dict[str, Any] = {"expression": text, "context": "watch"}
        if frame_id is not None:
            args["frameId"] = frame_id
        result = await self._dap("evaluate", args)
        reference = (result or {}).get("memoryReference")
        if reference:
            return str(reference)
        # cppdbg often returns pointers as "0x55… <symbol>"; take the address.
        value = str((result or {}).get("result", "")).strip()
        token = value.split()[0] if value else ""
        if token.startswith("0x"):
            return token
        raise BridgeError(
            f"could not resolve {expression!r} to an address (evaluated to "
            f"{value!r}). Pass a hex address, or an expression like '&buf'."
        )

    async def read_memory(
        self,
        memory_reference: str,
        *,
        count: int = 16,
        offset: int = 0,
        word_size: int = 8,
        per_row: int | None = None,
    ) -> dict[str, Any]:
        """Dump a range of memory in hex, rendered as a hex + ASCII dump
        rather than the raw base64 DAP actually carries."""
        if word_size not in WORD_SIZES:
            raise ValueError(
                f"word_size must be one of {WORD_SIZES} "
                "(4 = word, 8 = giant word, as in GDB's x/ sizes)"
            )
        reference = await self._resolve_address(memory_reference)
        byte_count = max(1, int(count)) * word_size
        result = await self._dap(
            "readMemory",
            {
                "memoryReference": reference,
                "count": byte_count,
                "offset": int(offset),
            },
        )
        encoded = (result or {}).get("data") or ""
        address = (result or {}).get("address") or reference
        try:
            data = base64.b64decode(encoded)
        except (ValueError, binascii.Error) as exc:
            raise BridgeError(f"bridge returned undecodable memory: {exc}") from exc

        try:
            base = int(str(address), 16)
        except ValueError:
            base = 0

        rows = build_word_rows(data, base, word_size, per_row)
        return {
            "expression": memory_reference,
            "address": address,
            "word_size": word_size,
            "bytes_read": len(data),
            "unreadable_bytes": (result or {}).get("unreadableBytes"),
            "rows": rows,
            "dump": truncate(render_rows(rows), 8000),
        }

    async def list_globals(
        self, *, pattern: str | None = None, include_values: bool = False
    ) -> dict[str, Any]:
        """Enumerate globals via GDB's own symbol table, through `-exec`.

        DAP has no request for this -- cppdbg exposes only Locals and Registers
        scopes -- so it goes through the raw GDB escape.
        """
        listing = await self.exec_gdb(
            f"info variables {pattern or ''}".strip(), tool_name="vsc_globals"
        )
        text = listing.get("output", "")
        names = _parse_info_variables(text)
        # `info variables` matches anywhere in the name, so a search for "g_"
        # also returns glibc's "__evoke_link_warning_sigreturn". Those carry
        # source files too, so "has debug info" does not separate them -- but a
        # name that *starts* with the pattern is almost always what was meant.
        if pattern:
            try:
                prefix = re.compile(pattern.lstrip("^"))
            except re.error:
                prefix = None
            if prefix is not None:
                names.sort(  # stable, so ties keep GDB's ordering
                    key=lambda entry: 0
                    if prefix.match(entry.get("name", ""))
                    else 1
                )

        out: dict[str, Any] = {
            "pattern": pattern,
            "count": len(names),
            "globals": names,
            "listing": truncate(text, 8000),
        }
        if pattern and len(names) > 25:
            out["note"] = (
                f"{len(names)} matches: `info variables` takes an *unanchored* "
                f"regex, so {pattern!r} also matches mid-name (e.g. 'g_' hits "
                "'__evoke_link_warning_sigreturn' inside glibc). Anchor it "
                f"-- '^{pattern.lstrip('^')}' -- to see only your own globals."
            )
        if include_values:
            for entry in names[:100]:
                try:
                    value = await self.evaluate(entry["name"])
                    entry["value"] = value["value"]
                except BridgeError as exc:
                    entry["value_error"] = str(exc)
        if not names and not text.strip():
            out["note"] = (
                "No output. cppdbg routes some -exec output to the Debug "
                "Console; check it there, or use gdb_globals against a core dump."
            )
        return out
