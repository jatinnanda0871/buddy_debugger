"""An in-process stand-in for the VS Code bridge extension.

Serves the same HTTP contract as vscode-extension/extension.js over loopback
TCP, so the Python client can be tested without VS Code.  Tests script it by
pushing events and setting DAP responses.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any


class FakeBridge:
    def __init__(self, *, token: str = "test-token") -> None:
        self.token = token
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

        self.seq = 0
        self.events: list[dict[str, Any]] = []
        self._wakeup = asyncio.Event()

        # Scriptable state
        self.sessions: list[dict[str, Any]] = []
        self.active_session_id: str | None = None
        self.workspace_folders: list[dict[str, str]] = []
        self.dap_responses: dict[str, Any] = {}
        self.program_output = ""
        self.breakpoints: list[dict[str, Any]] = []
        self.configs: list[dict[str, Any]] = []
        self.launch_should_fail = False

        #: every DAP command the client sent, for assertions
        self.dap_calls: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[str] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def discovery_info(self, **overrides: Any) -> dict[str, Any]:
        info = {
            "pid": os.getpid(),
            "token": self.token,
            "transport": "tcp",
            "host": "127.0.0.1",
            "port": self.port,
            "startedAt": int(time.time() * 1000),
            "workspaceName": "test",
            "workspaceFolders": [f["path"] for f in self.workspace_folders],
        }
        info.update(overrides)
        return info

    # -- scripting ----------------------------------------------------------

    def push_event(self, type_: str, **body: Any) -> dict[str, Any]:
        self.seq += 1
        event = {"seq": self.seq, "time": int(time.time() * 1000), "type": type_}
        event.update(body)
        self.events.append(event)
        self._wakeup.set()
        self._wakeup = asyncio.Event()
        return event

    def set_stopped(
        self, *, reason: str = "breakpoint", thread_id: int = 1, session_id: str = "s1"
    ) -> None:
        for session in self.sessions:
            if session["id"] == session_id:
                session["stopped"] = True
                session["lastStop"] = {"reason": reason, "threadId": thread_id}
                session["threadId"] = thread_id
        self.push_event("stopped", reason=reason, threadId=thread_id, sessionId=session_id)

    def add_session(
        self, *, id: str = "s1", name: str = "Debug", type: str = "cppdbg"
    ) -> dict[str, Any]:
        session = {
            "id": id,
            "name": name,
            "type": type,
            "workspaceFolder": None,
            "stopped": False,
            "lastStop": None,
            "threadId": None,
        }
        self.sessions.append(session)
        self.active_session_id = id
        return session

    # -- HTTP ---------------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return

        text = head.decode("utf-8", "replace")
        lines = text.split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                headers[key.lower()] = value

        body: dict[str, Any] = {}
        length = int(headers.get("content-length", 0) or 0)
        if length:
            raw = await reader.readexactly(length)
            body = json.loads(raw.decode("utf-8") or "{}")

        self.requests.append(f"{method} {target}")

        if headers.get("authorization") != f"Bearer {self.token}":
            await self._respond(writer, 401, {"ok": False, "error": "bad token"})
            return

        path, _, query_string = target.partition("?")
        query = {}
        for pair in query_string.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
                query[key] = value

        try:
            payload = await self._route(method, path, query, body)
            await self._respond(writer, 200, payload)
        except Exception as exc:  # noqa: BLE001 - mirrors the extension
            await self._respond(writer, 400, {"ok": False, "error": str(exc)})

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        content = json.dumps(payload).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(content)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(head + content)
        await writer.drain()
        writer.close()

    async def _route(
        self, method: str, path: str, query: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        if path == "/status":
            return {
                "ok": True,
                "lastSeq": self.seq,
                "activeSessionId": self.active_session_id,
                "sessions": self.sessions,
                "workspaceFolders": self.workspace_folders,
            }

        if path == "/request":
            command = body["command"]
            args = body.get("args", {})
            self.dap_calls.append((command, args))
            if command not in self.dap_responses:
                raise RuntimeError(f"unscripted DAP command: {command}")
            response = self.dap_responses[command]
            if callable(response):
                response = response(args)
            return {"ok": True, "command": command, "body": response}

        if path == "/events":
            since = int(query.get("since", 0))
            wait = float(query.get("wait", 0))
            matching = [e for e in self.events if e["seq"] > since]
            if not matching and wait > 0:
                waiter = self._wakeup
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                matching = [e for e in self.events if e["seq"] > since]
            return {"ok": True, "events": matching, "lastSeq": self.seq}

        if path == "/output":
            out, self.program_output = self.program_output, ""
            return {"ok": True, "output": out}

        if path == "/configs":
            return {
                "ok": True,
                "workspaces": [
                    {
                        "folder": "test",
                        "path": "/tmp/ws",
                        "configurations": self.configs,
                    }
                ],
            }

        if path == "/launch":
            if self.launch_should_fail:
                raise RuntimeError("VS Code refused to start the configuration")
            session = self.add_session(name=body.get("name", "Debug"))
            self.push_event("sessionStarted", sessionId=session["id"])
            return {"ok": True, "started": True, "lastSeq": self.seq, "session": session}

        if path == "/stop":
            self.sessions.clear()
            self.active_session_id = None
            return {"ok": True, "stopped": True}

        if path == "/breakpoints":
            if method == "GET":
                return {"ok": True, "breakpoints": self.breakpoints}
            action = body.get("action", "add")
            if action == "clear":
                self.breakpoints.clear()
            elif action == "add":
                self.breakpoints.extend(body.get("breakpoints", []))
            return {"ok": True, "breakpoints": self.breakpoints}

        raise RuntimeError(f"unknown route {path}")
