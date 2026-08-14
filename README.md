# gdb-mcp

Give an AI agent real control of a C/C++ debugger. Two backends, one MCP server:

**`vsc_*` — the VS Code session you're already in.** A companion extension
exposes the live debug session over DAP. Use this when you pressed F5, or when
the agent should launch the project the way F5 does.

**`gdb_*` — a standalone GDB the agent owns.** Spawns `gdb --interpreter=mi3`
and speaks GDB/MI. Use this for core dumps, headless work, and attaching to
processes VS Code isn't involved with.

```
                         ┌── vsc_* ──▶ bridge extension ──▶ VS Code debug session
your AI tool ──MCP/stdio──┤                                    │ (cppdbg → gdb)
                         │                                     ▼
                         │                                target program
                         │
                         └── gdb_* ──▶ gdb --interpreter=mi3 ──▶ target / core dump
```

**Why two?** Linux permits exactly one ptrace tracer per process. Once VS Code's
adapter is attached to your program, a second `gdb attach` fails outright — so a
live F5 session can only be reached through the adapter that already owns it.
Core dumps and headless runs have no such constraint, and a directly-owned GDB
gives more control there.

Dependencies are just the MCP SDK — the debugger layers use nothing but the
stdlib (no `pygdbmi`), and the VS Code extension is zero-dependency CommonJS
with no build step, so this drops onto a locked-down box cleanly.

## Install

```bash
git clone <your-remote> gdb_mcp && cd gdb_mcp
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # add '.[dev]' for the tests
```

Requires Python 3.10+ and `gdb` on `PATH` (set `GDB_PATH` to override).

Then install the VS Code extension — no build step, zero dependencies:

```bash
mkdir -p ~/.vscode/extensions/gdb-mcp-bridge-0.1.0
cp -r vscode-extension/* ~/.vscode/extensions/gdb-mcp-bridge-0.1.0/
```

Use `~/.vscode-server/extensions/` on a Remote-SSH or Dev Containers host — it
must live where the debugger runs, not on your laptop. Reload the window, then
run **GDB MCP Bridge: Show Status** to confirm. See
[vscode-extension/README.md](vscode-extension/README.md) for the HTTP contract
and security model.

The surface is 7 dispatcher tools, not one per operation: `gdb_session` /
`gdb_exec` / `gdb_breakpoint` / `gdb_inspect` cover the standalone-GDB
backend, `vsc_session` / `vsc_exec` / `vsc_inspect` cover the VS Code bridge.
Each takes an `operation` field (e.g. `gdb_exec(operation="step")`) plus that
operation's own arguments — see [Tools](#tools) below for the full list.
`GDB_MCP_TOOLS` still trims the surface: `vscode` exposes only `vsc_*`, `gdb`
only `gdb_*`, `all` (default) exposes both.

## Running it

```bash
gdb-mcp              # console script
python -m gdb_mcp    # equivalent
```

It speaks MCP over stdio. Registration depends on your client; for a
`.mcp.json`-style config:

```json
{
  "mcpServers": {
    "gdb": {
      "command": "/path/to/gdb_mcp/.venv/bin/gdb-mcp",
      "env": {
        "GDB_PATH": "/usr/bin/gdb",
        "GDB_MCP_TOOLS": "all"
      }
    }
  }
}
```

Every `gdb_*` tool takes `session` — pass different names to hold a core dump
and a live process open at the same time. Omit it and you get `"default"`.

## The two workflows this is built for

### 1. You hit an error in your own F5 session

You launched with F5 and you're sitting at a breakpoint or a crash. Ask the
agent for help; it works on *your* live session, no relaunch, no reproduction:

```
vsc_session(operation="status")                        → stopped, reason, frame, surrounding source
vsc_inspect(operation="backtrace")                      → call stack with frame ids
vsc_inspect(operation="frame", frame_id=id)             → locals and args, structs expanded one level
vsc_inspect(operation="eval", expression="hdr->len")
vsc_inspect(operation="raw", command="thread apply all bt")   ← raw gdb, via cppdbg's -exec
```

Nothing here disturbs your session — you keep your breakpoints and your place.

### 2. The agent made a change and wants to verify the fix

The agent presses F5 for you. `vsc_session(operation="launch")` calls
`vscode.debug.startDebugging`, which is the *same code path* as the F5 key — so
your `preLaunchTask` (the build), your `envFile`, your args and `cwd` all apply
exactly as configured. No re-implementation of `${workspaceFolder}` resolution
that could drift from what VS Code actually does.

```
vsc_session(operation="configs")                                  → list launch.json configurations
vsc_inspect(operation="set_breakpoints", breakpoints=[{file, line}])  → appear in your editor gutter
vsc_session(operation="launch", name="Debug my app")               → builds, launches, waits for first stop
vsc_exec(operation="continue") / vsc_exec(operation="step") / vsc_inspect(operation="output")
vsc_session(operation="terminate")
```

## Tools

7 dispatcher tools, each taking an `operation` field plus that operation's
own arguments — e.g. `gdb_exec(operation="step", kind="next")`. Grouped by
what they do, not by original tool count: session lifecycle, execution
control, breakpoint management, and read-only inspection are different
enough in shape that folding them into fewer tools would just make both the
code and the model's job harder. Every `gdb_*` tool also takes `session` (see
above); `vsc_*` tools don't, since there's only one VS Code session.

### Standalone GDB (`gdb_*`)

| Tool | `operation` values | Purpose |
| --- | --- | --- |
| `gdb_session` | `start`, `stop`, `status`, `attach`, `load_core`, `record` | Open/close a session (`stop` **detaches** by default); where am I; attach to a live PID (**stops that process**); post-mortem on a core dump; start/stop execution recording |
| `gdb_exec` | `run`, `continue`, `interrupt`, `step`, `next`, `finish`, `stepi`, `nexti`, `until`, `return`, `reverse-continue`, `reverse-step`, `reverse-next`, `reverse-finish` | Execution control, forward and backward (reverse requires `gdb_session(operation="record")` first) |
| `gdb_breakpoint` | `set`, `watch`, `list`, `delete`, `enable`, `disable`, `modify` | Create, list, or edit breakpoints/watchpoints — `modify`/`enable`/`disable` act on an existing breakpoint without deleting it |
| `gdb_inspect` | `backtrace`, `frame`, `eval`, `threads`, `select_thread`, `registers`, `memory`, `globals`, `disassemble`, `source`, `program_output`, `raw` | Stack, locals + args, expressions, threads, registers, an address-range hex dump, **global variables** (`frame` shows only locals), disassembly, source, captured stdout/stderr, and a raw-GDB-command escape hatch |

### VS Code session (`vsc_*`)

| Tool | `operation` values | Purpose |
| --- | --- | --- |
| `vsc_session` | `status`, `configs`, `launch`, `terminate` | **`status` is the preferred first call.** List and run launch.json configs |
| `vsc_exec` | `continue`, `next`, `step`, `stepIn`, `stepOut`, `finish`, `pause`, `wait_stop` | Execution control |
| `vsc_inspect` | `threads`, `backtrace`, `frame`, `eval`, `breakpoints`, `set_breakpoints`, `output`, `disassemble`, `memory`, `globals`, `raw` | Stack, locals + args, expressions, threads, breakpoints (visible in the editor), captured stdout/stderr, an address-range hex dump, **global variables** (`frame` shows only locals), and a raw-GDB-command escape hatch via cppdbg's `-exec` |

## Globals and memory ranges

**Globals are not locals.** `gdb_inspect(operation="frame")` /
`vsc_inspect(operation="frame")` use `-stack-list-variables` and DAP `scopes`,
which return *only* locals and arguments — globals are invisible there.
`operation="globals"` enumerates them from the symbol table instead:

```
gdb_inspect(operation="globals", pattern="g_conn", include_values=true)
  → [{name: "g_connection_count", type: "int", file: "src/server.c",
      line: 12, value: "17"}, …]
```

Always pass a `pattern` regex — without one you get every global in every linked
library, thousands of symbols. File-scope statics with colliding names need
qualification: `gdb_inspect(operation="eval", expression="'server.c'::g_config")`.

On the VS Code side, DAP has no symbol-listing request, so
`vsc_inspect(operation="globals")` routes through `-exec info variables` and
parses the result.

**Memory ranges.** One operation per backend, always hex.
`gdb_inspect(operation="memory")` reads `count` words of `word_size` bytes,
with an ASCII gutter alongside:

```
gdb_inspect(operation="memory", address="$sp", count=8)
0x00007ffd0000  0x00007ffff7a2d0b0  0x0000000000000001  |................|
0x00007ffd0010  0x00005555555551a9  0x0000000000000000  |.QUUUU..........|
```

`word_size` follows GDB's own `x/` size letters — **4 = word, 8 = giant word**
(the default, and what pointers are on x86-64). A word is the smallest unit;
there is no byte or halfword view. Rows are 16 bytes wide either way.

`vsc_inspect(operation="memory")` is the same operation against the VS Code
session, and additionally accepts expressions (`&buf`, `pkt->payload`) rather
than only raw addresses — it evaluates them to an address first. It renders
the same rows, not the base64 blob DAP actually carries.

**Everything else is hex too.** Both backends set `set output-radix 16`, so
evaluated expressions, locals and register values print in hex and agree with
the memory dumps rather than mixing radices — `gdb_inspect(operation="eval")`
and `vsc_inspect(operation="eval")` return `0x11` for the same variable, not
`0x11` and `17`.

For `vsc_*` this is applied once per GDB-backed session, which also changes
what **your** Variables pane and hover tooltips show, since it is your session.
Set `GDB_MCP_VSCODE_HEX=0` to leave the editor in decimal; the `gdb_*` backend
is unaffected either way. Adapters with no GDB underneath (debugpy, cppvsdbg)
are never touched.

## Design notes

Three things that make or break an agent-driven debugger:

**Execution commands are asynchronous — in both backends.** `-exec-continue`
answers `^running` *immediately*; the program halting is a separate `*stopped`
record that arrives later. DAP behaves the same way: `continue` returns at once
and a `stopped` event follows. Tools that return on the request's own response
hand the model a stale, wrong view of the world.

Both backends therefore wait for the *real* stop — GDB/MI via a stop queue, DAP
via long-polling `/events` from a sequence cursor captured before the resume (so
a stop from a previous run can't satisfy the current wait). On timeout both
return `state: "running"` pointing at `gdb_exec(operation="interrupt")` /
`vsc_exec(operation="pause")`. Neither ever fabricates a stop.

**Stops answer the whole question at once.** Every stop returns reason, thread,
breakpoint number, frame, *and* the surrounding source lines, so the model needs
one call rather than four to know where it is.

**Locals are diffed, not re-sent.** Every `gdb_exec` operation and its
`vsc_exec` equivalents fold the current frame's locals into the stop result.
The first stop in a scope returns all of them, under `variables`; every
subsequent stop in the *same* scope (same function) instead returns
`changed_variables` — only the ones whose value moved, plus an
`unchanged_count` — so stepping through a loop doesn't re-send every unchanged
local on every iteration. A scope change (a different function) resets the
baseline and returns the full list again.

**The inferior gets its own pty.** On a native Linux target the debugged program
shares GDB's terminal, so its `printf` output lands in the middle of the MI
stream and corrupts parsing. The session allocates a pty, points
`inferior-tty` at it, and buffers that output for
`gdb_inspect(operation="program_output")`.

Also: `pagination off` at startup (otherwise GDB blocks forever on
`---Type <return> to continue---`), `-nx` by default for reproducibility (pass
`use_init_file=true` if your team's `.gdbinit` installs needed pretty-printers),
and an MI-version fallback to `mi2` for GDB older than 8.1.

## Safety

The defaults assume you may point this at something you care about.

- **`gdb_inspect(operation="eval")` blocks inferior function calls.** In GDB,
  `print some_func()` really executes that function inside the target
  process. On a process you attached to in production, that is a live side
  effect. Set `allow_function_calls=true` to opt in per call.
- **`gdb_session(operation="stop")` detaches, it doesn't kill.** Pass
  `kill=true` deliberately.
- **`gdb_session(operation="attach")` freezes the target** until you continue
  it. On a production service that is downtime — an agent should be told so
  in its system prompt.
- **`gdb_session(operation="record")` has real overhead.** Process
  record-and-replay slows execution and its memory cost grows with how long
  it runs — start it only when you actually need `gdb_exec`'s `reverse-*`
  operations, and stop it when you're done. It is `gdb_*`-only: there is no
  `vsc_*` equivalent, since DAP's reverse-debugging requests aren't reliably
  supported by the adapters this project targets. A GDB-backed `vsc_*`
  session can still reach the same underlying commands via
  `vsc_inspect(operation="raw", command="record")` /
  `vsc_inspect(operation="raw", command="reverse-step")`, just without the
  structured stop result.
- **`gdb_exec`'s step/reverse operations can hang for minutes if you step
  over a call into code with no line info** (a vectorized libc routine like
  `strlen` is the common case) while recording is active. GDB's `record full`
  target single-steps and snapshots *every instruction*, including inside the
  callee, and optimized SIMD implementations can take real gdb minutes to
  single-step through this way — confirmed by hand against gdb 15.1, where a
  single `next` over one `strlen()` call never returned within 60s. Step past
  such calls *before* calling `gdb_session(operation="record", action="start")`,
  not after.
- **`gdb_inspect(operation="raw")` and `vsc_inspect(operation="raw")` are
  unrestricted.** Both reach `shell`, `set var`, `call`. Drop `gdb_inspect` /
  `vsc_inspect` from the list if you want a read-only posture — note this also
  drops the rest of their read-only operations, since `raw` isn't split out
  into its own tool.
- **The bridge socket is `0600`** in your `$TMPDIR`, with a fresh 32-byte token
  per activation, so other users on a shared box cannot connect.

Anyone who can call these tools can read all memory of the target process
(credentials, keys, customer data) and, via `operation="raw"`, run commands as
your user. Run it as an unprivileged user scoped to what it should debug.

## Linux setup gotchas

**Attaching** — most distros set `ptrace_scope=1`, which allows attaching only
to descendants:

```bash
cat /proc/sys/kernel/yama/ptrace_scope    # 1 = restricted
sudo sysctl -w kernel.yama.ptrace_scope=0 # session-wide, or grant CAP_SYS_PTRACE
```

In containers, add `--cap-add=SYS_PTRACE` (and often
`--security-opt seccomp=unconfined`).

**Core dumps** — make sure they're actually produced and find where they go:

```bash
ulimit -c unlimited
cat /proc/sys/kernel/core_pattern
```

If that pattern pipes to `systemd-coredump`, the files are not on disk where you
expect — extract one first:

```bash
coredumpctl list
coredumpctl dump <pid> --output=/tmp/core.1234
```

**Symbols** — build with `-g`. For system libraries install the matching
`-dbg`/`-debuginfo` packages, or point GDB at a symbol store with
`gdb_inspect(operation="raw", command="set debug-file-directory /path/to/debug")`.
Without them backtraces are just addresses and the model will guess.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

Three layers, and only the first runs everywhere:

| Layer | Needs | What it proves |
| --- | --- | --- |
| Unit + protocol | nothing | MI parsing, event/stop logic, MCP schemas and `isError` |
| Contract (`test_contract.py`) | nothing | `fake_bridge` still matches `extension.js` route-for-route |
| Live (`tests/live/`) | real gdb / real VS Code | the parts that actually break |

**The live tests are the ones that find bugs.** Every defect in this project so
far — the `aschar 46` gutter, the rejected `-data-disassemble` form, mcp 2.0,
CRLF from the pty, output lost on exit — passed the fakes and died on contact
with the real thing. A fake only proves the client agrees with *my assumptions
about* the real component.

`tests/live/test_live_gdb.py` compiles a C++ target with clang and drives real
GDB. It skips cleanly unless both `gdb` and `clang++` are on PATH, so it runs
unattended anywhere the toolchain exists.

`tests/live/test_live_bridge.py` and `test_live_bridge_cpp.py` drive real VS
Code debug sessions, so they need an explicit opt-in as well as a discoverable
bridge — otherwise merely having the editor open would let `pytest` hijack it:

```bash
GDB_MCP_LIVE_BRIDGE=1 pytest tests/live/
```

The cppdbg file is the one that matters for C++: it is the only place
`vsc_inspect`'s `raw`, `globals`, `memory` and `disassemble` operations do
real work (debugpy can only prove they refuse cleanly). On Linux it builds and debugs
locally; on Windows it builds in WSL and reaches it through cppdbg's
`pipeTransport`, so gdb runs on Linux while VS Code stays on Windows. Point
`GDB_MCP_CPP_PROGRAM` at a prebuilt binary to skip the build, and
`GDB_MCP_WSL_DISTRO` to pick a distro.

The contract tests exist because the fake is otherwise free to drift: they
compare the extension's route table, discovery descriptor and event fields
against both the fake and the client, statically, with no Node required.

Typical counts: 171 passed / 35 skipped on Linux with gdb; 130 / 76 on Windows
without it; 165 / 41 on Windows with the bridge opted in and WSL available.

Standalone GDB smoke test:

```bash
printf 'int f(int x){return x*2;}\nint main(){return f(21);}\n' > /tmp/t.c
gcc -g -O0 /tmp/t.c -o /tmp/t
python -c "
import asyncio; from gdb_mcp import Debugger
async def m():
    d = Debugger(); await d.start(binary='/tmp/t')
    await d.set_breakpoint('f'); print(await d.run())
    print(await d.evaluate('x')); await d.stop()
asyncio.run(m())"
```

Bridge smoke test — install the extension, open your project, press F5, stop at
a breakpoint, then:

```bash
python -c "
import asyncio, json
from gdb_mcp.vscode_bridge import VSCodeDebugger
async def m():
    v = VSCodeDebugger()
    print(json.dumps(await v.status(), indent=2))
    print(json.dumps(await v.backtrace(), indent=2))
asyncio.run(m())"
```

If that prints your stopped frame and call stack, the whole path works. If it
raises `BridgeNotFound`, the extension isn't loaded — check
**GDB MCP Bridge: Show Status** and that you installed it on the machine where
the debugger runs.
