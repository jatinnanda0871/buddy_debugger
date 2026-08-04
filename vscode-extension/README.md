# GDB MCP Bridge (VS Code extension)

Publishes the debug session VS Code already owns to a local AI agent.

## Why this exists

Linux permits exactly **one ptrace tracer per process**. When you press F5, the
C/C++ extension's debug adapter (cppdbg → gdb) attaches to your program. A
second `gdb attach <pid>` then fails with `Operation not permitted`. So an agent
cannot join the session you are sitting in from the outside — the only way in is
through the adapter that is already attached. That is what this bridge does.

## Install

No build step. It is plain CommonJS with zero dependencies — nothing to compile,
no `npm install`.

```bash
# Linux / macOS
mkdir -p ~/.vscode/extensions/gdb-mcp-bridge-0.1.0
cp -r vscode-extension/* ~/.vscode/extensions/gdb-mcp-bridge-0.1.0/
```

Use `~/.vscode-server/extensions/` instead if you are on a Remote-SSH or
Dev Containers host — it must live where the *debugger* runs, not on your laptop.

Reload the window (`Developer: Reload Window`). Confirm with the command
**GDB MCP Bridge: Show Status** — you should see a `$(debug-alt) GDB MCP` item
in the status bar.

## How the agent finds it

On activation the extension listens on a Unix domain socket in `$TMPDIR`
(`chmod 0600`) and writes a descriptor to:

```
~/.gdb-mcp/bridges/<vscode-pid>.json
```

The Python client globs that directory, drops entries whose PID is dead, and
prefers a window whose workspace folder contains the path you are working in.
Several VS Code windows can run at once without colliding.

On Windows there are no Unix sockets, so it falls back to a loopback TCP port.

## HTTP contract

All routes need `Authorization: Bearer <token>` from the descriptor file.

| Route | Purpose |
| --- | --- |
| `GET /status` | Active session, stopped state, `lastSeq` event cursor |
| `POST /request` | `{command, args}` forwarded to `session.customRequest` (any DAP request) |
| `GET /events?since=N&wait=S` | Long-poll the event log; how the client waits for a real stop |
| `GET /output` | Drain the debuggee's stdout/stderr |
| `GET /configs` | launch.json configurations |
| `POST /launch` | `{name}` → `vscode.debug.startDebugging` (runs preLaunchTask, envFile, …) |
| `POST /stop` | Stop the session |
| `GET`/`POST /breakpoints` | Read/modify breakpoints in VS Code's UI |

The extension is deliberately thin: it forwards DAP, records events, and
controls launching. All interpretation lives in `gdb_mcp/vscode_bridge.py`,
which is where the tests are.

## Security

- The socket is `0600` and lives in your `$TMPDIR`; the descriptor file is
  `0600` under `~/.gdb-mcp/`. On a shared box other users cannot connect.
- A 32-byte bearer token is required regardless of transport, and is regenerated
  on every activation.
- Anything that can reach the socket can read your debuggee's memory and, via
  `-exec`, run arbitrary GDB commands — which includes `shell`. Treat access to
  it as equivalent to shell access as your user.
- Set `gdbMcpBridge.enabled: false` to turn it off without uninstalling.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `gdbMcpBridge.enabled` | `true` | Run the bridge server |
| `gdbMcpBridge.transport` | `auto` | `auto` \| `unix` \| `tcp` |
| `gdbMcpBridge.port` | `0` | TCP port when transport is `tcp`; 0 picks a free one |

## Commands

- **GDB MCP Bridge: Show Status** — endpoint and session count, opens the log
- **GDB MCP Bridge: Restart Server** — new socket and token
- **GDB MCP Bridge: Copy Endpoint Info** — endpoint JSON to the clipboard, for
  debugging the bridge itself
