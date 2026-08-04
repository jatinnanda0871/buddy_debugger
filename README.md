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

`GDB_MCP_TOOLS` trims the 44-tool surface if that's too much for your model to
choose between: `vscode` exposes only `vsc_*`, `gdb` only `gdb_*`, `all`
(default) exposes both.

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
vsc_status          → stopped, reason, frame, surrounding source
vsc_backtrace       → call stack with frame ids
vsc_frame(id)       → locals and args, structs expanded one level
vsc_eval("hdr->len")
vsc_exec("thread apply all bt")     ← raw gdb, via cppdbg's -exec
```

Nothing here disturbs your session — you keep your breakpoints and your place.

### 2. The agent made a change and wants to verify the fix

The agent presses F5 for you. `vsc_launch` calls
`vscode.debug.startDebugging`, which is the *same code path* as the F5 key — so
your `preLaunchTask` (the build), your `envFile`, your args and `cwd` all apply
exactly as configured. No re-implementation of `${workspaceFolder}` resolution
that could drift from what VS Code actually does.

```
vsc_configs                          → list launch.json configurations
vsc_set_breakpoints([{file, line}])  → appear in your editor gutter
vsc_launch("Debug my app")           → builds, launches, waits for first stop
vsc_continue / vsc_step / vsc_output
vsc_terminate
```

## Tools

### VS Code session (`vsc_*`)

| Tool | Purpose |
| --- | --- |
| `vsc_status` | **Start here.** Session active? stopped? current frame + source |
| `vsc_configs` / `vsc_launch` / `vsc_terminate` | List and run launch.json configs |
| `vsc_continue` / `vsc_step` / `vsc_pause` / `vsc_wait_stop` | Execution control |
| `vsc_backtrace` / `vsc_frame` / `vsc_eval` | Stack, locals + args, expressions |
| `vsc_globals` | **Enumerate globals** — `vsc_frame` shows only locals |
| `vsc_threads` | Threads |
| `vsc_breakpoints` / `vsc_set_breakpoints` | Breakpoints, visible in the editor |
| `vsc_output` | Drain the program's stdout/stderr |
| `vsc_memory` | Address-range dump in hex words with an ASCII gutter |
| `vsc_disassemble` | Disassembly at a `pc` |
| `vsc_exec` | Escape hatch: raw GDB command via `-exec` |

### Standalone GDB (`gdb_*`)

| Tool | Purpose |
| --- | --- |
| `gdb_start` / `gdb_stop` | Open/close a session; `gdb_stop` **detaches** by default |
| `gdb_status` | Where am I: running or stopped, frame, source context |
| `gdb_attach` | Attach to a live PID (**stops that process**) |
| `gdb_load_core` | Post-mortem on a core dump; returns the crash backtrace |
| `gdb_run` / `gdb_continue` / `gdb_step` / `gdb_interrupt` | Execution control |
| `gdb_break` / `gdb_watch` / `gdb_breakpoints` / `gdb_delete_breakpoint` | Breakpoints |
| `gdb_backtrace` / `gdb_frame` / `gdb_eval` | Stack, locals + args, expressions |
| `gdb_globals` | **Enumerate globals** with types and optional values |
| `gdb_threads` / `gdb_select_thread` | Threads |
| `gdb_memory` | Address-range dump in hex words with an ASCII gutter |
| `gdb_registers` / `gdb_disassemble` / `gdb_source` | Low-level inspection |
| `gdb_program_output` | Drain the inferior's stdout/stderr |
| `gdb_raw` | Escape hatch: any GDB CLI command |

## Globals and memory ranges

**Globals are not locals.** `gdb_frame` / `vsc_frame` use
`-stack-list-variables` and DAP `scopes`, which return *only* locals and
arguments — globals are invisible there. `gdb_globals` enumerates them from the
symbol table:

```
gdb_globals(pattern="g_conn", include_values=true)
  → [{name: "g_connection_count", type: "int", file: "src/server.c",
      line: 12, value: "17"}, …]
```

Always pass a `pattern` regex — without one you get every global in every linked
library, thousands of symbols. File-scope statics with colliding names need
qualification: `gdb_eval("'server.c'::g_config")`.

On the VS Code side, DAP has no symbol-listing request, so `vsc_globals` routes
through `-exec info variables` and parses the result.

**Memory ranges.** One tool per backend, always hex. `gdb_memory` reads `count`
words of `word_size` bytes, with an ASCII gutter alongside:

```
gdb_memory("$sp", count=8)
0x00007ffd0000  0x00007ffff7a2d0b0  0x0000000000000001  |................|
0x00007ffd0010  0x00005555555551a9  0x0000000000000000  |.QUUUU..........|
```

`word_size` follows GDB's own `x/` size letters — **4 = word, 8 = giant word**
(the default, and what pointers are on x86-64). A word is the smallest unit;
there is no byte or halfword view. Rows are 16 bytes wide either way.

`vsc_memory` is the same tool against the VS Code session, and additionally
accepts expressions (`&buf`, `pkt->payload`) rather than only raw addresses — it
evaluates them to an address first. It renders the same rows, not the base64
blob DAP actually carries.

**Everything else is hex too.** Sessions start with `set output-radix 16`, so
evaluated expressions, locals and register values all print in hex and agree
with the memory dumps rather than mixing radices.

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
return `state: "running"` pointing at `gdb_interrupt` / `vsc_pause`. Neither
ever fabricates a stop.

**Stops answer the whole question at once.** Every stop returns reason, thread,
breakpoint number, frame, *and* the surrounding source lines, so the model needs
one call rather than four to know where it is.

**The inferior gets its own pty.** On a native Linux target the debugged program
shares GDB's terminal, so its `printf` output lands in the middle of the MI
stream and corrupts parsing. The session allocates a pty, points
`inferior-tty` at it, and buffers that output for `gdb_program_output`.

Also: `pagination off` at startup (otherwise GDB blocks forever on
`---Type <return> to continue---`), `-nx` by default for reproducibility (pass
`use_init_file=true` if your team's `.gdbinit` installs needed pretty-printers),
and an MI-version fallback to `mi2` for GDB older than 8.1.

## Safety

The defaults assume you may point this at something you care about.

- **`gdb_eval` blocks inferior function calls.** In GDB, `print some_func()`
  really executes that function inside the target process. On a process you
  attached to in production, that is a live side effect. Set
  `allow_function_calls=true` to opt in per call.
- **`gdb_stop` detaches, it doesn't kill.** Pass `kill=true` deliberately.
- **`gdb_attach` freezes the target** until you continue it. On a production
  service that is downtime — an agent should be told so in its system prompt.
- **`gdb_raw` and `vsc_exec` are unrestricted.** Both reach `shell`, `set var`,
  `call`. Drop them from the list if you want a read-only posture.
- **The bridge socket is `0600`** in your `$TMPDIR`, with a fresh 32-byte token
  per activation, so other users on a shared box cannot connect.

Anyone who can call these tools can read all memory of the target process
(credentials, keys, customer data) and, via `gdb_raw` / `vsc_exec`, run commands
as your user. Run it as an unprivileged user scoped to what it should debug.

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
`gdb_raw("set debug-file-directory /path/to/debug")`. Without them backtraces
are just addresses and the model will guess.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

98 tests, requiring neither `gdb` nor VS Code:

- the MI parser runs against captured real gdb 12/13 output
- the GDB session layer runs against `tests/fake_gdb.py`, a stub speaking enough
  MI to reproduce the async-stop handshake, token interleaving, and the
  never-stops-until-interrupted case
- the bridge client runs against `tests/fake_bridge.py`, which serves the same
  HTTP contract as the extension, covering stop-event waiting, stale-event
  rejection, discovery of dead VS Code windows, and DAP variable expansion

**This exercises the protocol layers, not GDB or VS Code themselves.** The
extension in particular has never been executed — there was no Node toolchain
available when it was written. Verify both on your box before trusting them.

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
