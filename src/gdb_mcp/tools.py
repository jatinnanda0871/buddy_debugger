"""The standalone-GDB tool surface, backing the `gdb_*` MCP tools.

Each method maps to one tool in `gdb_mcp.server` and returns JSON-serialisable
data, raising ``GdbError`` / ``ValueError`` on failure for the server to turn
into an error payload.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from .session import GdbError, GdbSession, StopEvent

#: Cap on any single blob of text handed back to the model.
MAX_TEXT = 12_000
DEFAULT_STOP_TIMEOUT = 30.0

STEP_KINDS = {
    "step": "-exec-step",
    "next": "-exec-next",
    "finish": "-exec-finish",
    "stepi": "-exec-step-instruction",
    "nexti": "-exec-next-instruction",
    "return": "-exec-return",
    "until": "-exec-until",
}


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    keep = limit // 2
    dropped = len(text) - 2 * keep
    return (
        text[:keep]
        + f"\n\n... [{dropped} characters truncated] ...\n\n"
        + text[-keep:]
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


#: Readable words-per-row, chosen so every row spans 16 bytes.
_COLS_BY_WORD_SIZE = {4: 4, 8: 2}

#: A word is the smallest unit a dump is grouped into. These follow GDB's own
#: `x` size letters, where w (word) is 4 bytes and g (giant word) is 8.
WORD_SIZES = (4, 8)


def to_int(value: Any) -> int | None:
    """Parse an integer GDB may have printed in decimal or (radix 16) hex."""
    if isinstance(value, int):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for base in (10, 16):
        try:
            return int(text, base)
        except ValueError:
            continue
    return None


def decode_words(data: bytes, word_size: int) -> list[str]:
    """Group a byte range into little-endian hex words."""
    if word_size not in WORD_SIZES:
        return []
    usable = len(data) - (len(data) % word_size)
    return [
        "0x" + data[offset : offset + word_size][::-1].hex()
        for offset in range(0, usable, word_size)
    ]


def build_word_rows(
    data: bytes,
    base_address: int = 0,
    word_size: int = 8,
    per_row: int | None = None,
) -> list[dict[str, Any]]:
    """Lay a byte range out as rows of hex words with an ASCII gutter.

    Produces the same row shape GDB's -data-read-memory returns, so both
    backends render through render_rows() and look identical.
    """
    if word_size not in WORD_SIZES:
        return []
    cols = int(per_row or _COLS_BY_WORD_SIZE.get(word_size, 4))
    stride = word_size * cols
    rows: list[dict[str, Any]] = []
    limit = len(data) - (len(data) % word_size)
    for offset in range(0, limit, stride):
        chunk = data[offset : min(offset + stride, limit)]
        if not chunk:
            break
        rows.append(
            {
                "addr": f"0x{base_address + offset:012x}",
                "data": decode_words(chunk, word_size),
                "ascii": "".join(
                    chr(b) if 32 <= b < 127 else "." for b in chunk
                ),
            }
        )
    return rows


def render_rows(rows: list[dict[str, Any]]) -> str:
    """Render GDB's -data-read-memory rows as an aligned table."""
    # Widths must be computed across every row, not per row, or the columns
    # stagger and the dump becomes hard to read down a column.
    value_width = max(
        (len(str(v)) for row in rows for v in (row.get("data") or [])), default=0
    )
    addr_width = max((len(str(row.get("addr", ""))) for row in rows), default=0)
    columns = max((len(row.get("data") or []) for row in rows), default=0)

    lines = []
    for row in rows:
        values = [str(v) for v in (row.get("data") or [])]
        cells = " ".join(v.rjust(value_width) for v in values)
        # pad short trailing rows so the ASCII gutter stays in one column
        missing = columns - len(values)
        if missing > 0:
            cells += " " * (missing * (value_width + 1))
        line = f"{str(row.get('addr', '')).ljust(addr_width)}  {cells}"
        if row.get("ascii"):
            line += f"  |{row['ascii']}|"
        lines.append(line)
    return "\n".join(lines)


def source_context(
    fullname: str | None, line: Any, *, before: int = 6, after: int = 6
) -> list[dict[str, Any]] | None:
    """Read source lines around `line`, if the file is readable locally."""
    if not fullname or line in (None, ""):
        return None
    lineno = to_int(line)
    if lineno is None:
        return None
    if not os.path.isfile(fullname):
        return None
    try:
        with open(fullname, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    lo = max(1, lineno - before)
    hi = min(len(lines), lineno + after)
    return [
        {
            "line": n,
            "current": n == lineno,
            "text": lines[n - 1].rstrip("\n"),
        }
        for n in range(lo, hi + 1)
    ]


def describe_stop(stop: StopEvent | None, *, running: bool) -> dict[str, Any]:
    """Compact 'where am I' summary, so the model needs one call, not four."""
    if stop is None:
        return {
            "state": "running" if running else "unknown",
            "note": (
                "The inferior is still running. Call gdb_interrupt to halt it, "
                "or gdb_continue again with a longer timeout."
            )
            if running
            else "No stop event was reported.",
        }

    payload = stop.payload
    out: dict[str, Any] = {"state": "stopped", "reason": stop.reason}

    if stop.exited:
        out["state"] = "exited"
        if "exit-code" in payload:
            out["exit_code"] = payload["exit-code"]
        out["note"] = "The inferior has exited; re-run it before inspecting state."
        return out

    for key, name in (
        ("thread-id", "thread_id"),
        ("bkptno", "breakpoint"),
        ("signal-name", "signal"),
        ("signal-meaning", "signal_meaning"),
        ("disp", "breakpoint_disposition"),
    ):
        if key in payload:
            out[name] = payload[key]

    frame = payload.get("frame")
    if isinstance(frame, dict):
        out["frame"] = {
            k: frame.get(k)
            for k in ("level", "addr", "func", "file", "fullname", "line")
            if frame.get(k) is not None
        }
        args = frame.get("args")
        if args:
            out["frame"]["args"] = args
        ctx = source_context(frame.get("fullname"), frame.get("line"))
        if ctx:
            out["source"] = ctx
    return out


class Debugger:
    """A registry of named GDB sessions (one 'default' session is enough for
    most workflows; extra names let you hold a core dump and a live process at
    the same time)."""

    def __init__(self, *, gdb_path: str = "gdb") -> None:
        self.gdb_path = gdb_path
        self.sessions: dict[str, GdbSession] = {}

    # -- session plumbing ---------------------------------------------------

    def get(self, name: str = "default") -> GdbSession:
        session = self.sessions.get(name)
        if session is None or not session.alive:
            raise ValueError(
                f"No active GDB session named {name!r}. Call gdb_start first."
            )
        return session

    async def _ensure_session(self, name: str) -> GdbSession:
        """Get the session, starting one if it does not exist yet.

        gdb_attach and gdb_load_core read like complete operations -- they name
        the target themselves -- so requiring a separate gdb_start first is a
        trap. They open their own session instead.
        """
        existing = self.sessions.get(name)
        if existing is not None and existing.alive:
            return existing
        self.sessions.pop(name, None)
        await self.start(session=name)
        return self.get(name)

    async def start(
        self,
        *,
        session: str = "default",
        binary: str | None = None,
        args: list[str] | str | None = None,
        cwd: str | None = None,
        gdb_path: str | None = None,
        use_init_file: bool = False,
    ) -> dict[str, Any]:
        existing = self.sessions.get(session)
        if existing is not None and existing.alive:
            raise ValueError(
                f"Session {session!r} is already running. Call gdb_stop first, "
                "or use a different session name."
            )

        gdb = GdbSession(
            gdb_path or self.gdb_path, cwd=cwd, use_init_file=use_init_file
        )
        await gdb.start()
        self.sessions[session] = gdb

        out: dict[str, Any] = {
            "session": session,
            "mi_version": gdb.mi_version,
            "inferior_tty": gdb.inferior_tty,
        }
        with contextlib.suppress(GdbError):
            out["gdb_version"] = (await gdb.console("show version")).strip().splitlines()[0]

        if binary:
            out.update(await self.load_binary(binary, args=args, session=session))
        return out

    async def load_binary(
        self,
        binary: str,
        *,
        args: list[str] | str | None = None,
        session: str = "default",
    ) -> dict[str, Any]:
        gdb = self.get(session)
        path = os.path.abspath(os.path.expanduser(binary))
        if not os.path.exists(path):
            raise ValueError(f"Binary not found: {path}")
        await gdb.send(f'-file-exec-and-symbols "{path}"')
        gdb.binary = path
        out: dict[str, Any] = {"binary": path}
        if args:
            argstr = args if isinstance(args, str) else " ".join(args)
            await gdb.send(f"-exec-arguments {argstr}")
            out["args"] = argstr
        with contextlib.suppress(GdbError):
            info = await gdb.console("info functions ^main$")
            out["has_main"] = "main" in info
        return out

    async def attach(
        self, pid: int, *, binary: str | None = None, session: str = "default"
    ) -> dict[str, Any]:
        gdb = await self._ensure_session(session)
        if binary:
            await self.load_binary(binary, session=session)
        gdb.drain_stops()
        await gdb.send(f"-target-attach {int(pid)}", timeout=60)
        gdb.attached_pid = int(pid)
        # Attaching stops the process; report where it landed.
        stop = gdb.last_stop or await gdb.wait_for_stop(timeout=5)
        out = {"attached_pid": int(pid), "stop": describe_stop(stop, running=gdb.running)}
        if stop is None:
            out["stop"] = await self.status(session=session)
        return out

    async def load_core(
        self, binary: str, core: str, *, session: str = "default"
    ) -> dict[str, Any]:
        gdb = await self._ensure_session(session)
        await self.load_binary(binary, session=session)
        core_path = os.path.abspath(os.path.expanduser(core))
        if not os.path.exists(core_path):
            raise ValueError(f"Core file not found: {core_path}")
        # No MI command for core files; the CLI one is the supported route.
        output = await gdb.console(f"core-file {core_path}", timeout=120)
        gdb.core_file = core_path
        result = {
            "core": core_path,
            "binary": gdb.binary,
            "gdb_output": truncate(output, 4000),
        }
        with contextlib.suppress(GdbError, ValueError):
            result["backtrace"] = (await self.backtrace(limit=20, session=session))[
                "frames"
            ]
        return result

    async def stop(
        self, *, session: str = "default", kill: bool = False
    ) -> dict[str, Any]:
        gdb = self.sessions.pop(session, None)
        if gdb is None:
            return {"session": session, "status": "no such session"}
        was_attached = gdb.attached_pid
        await gdb.close(kill=kill)
        return {
            "session": session,
            "status": "closed",
            "action": "killed"
            if kill
            else ("detached" if was_attached else "terminated"),
        }

    async def status(self, *, session: str = "default") -> dict[str, Any]:
        gdb = self.get(session)
        out: dict[str, Any] = {
            "session": session,
            "running": gdb.running,
            "binary": gdb.binary,
            "attached_pid": gdb.attached_pid,
            "core_file": gdb.core_file,
        }
        if gdb.running:
            out["state"] = "running"
            out["note"] = "Call gdb_interrupt to halt and inspect."
            return out
        with contextlib.suppress(GdbError):
            frame = (await gdb.send("-stack-info-frame")).payload.get("frame")
            if isinstance(frame, dict):
                out["frame"] = frame
                ctx = source_context(frame.get("fullname"), frame.get("line"))
                if ctx:
                    out["source"] = ctx
        if gdb.last_stop is not None:
            out["last_stop_reason"] = gdb.last_stop.reason
        return out

    # -- execution ----------------------------------------------------------

    async def run(
        self,
        *,
        session: str = "default",
        args: list[str] | str | None = None,
        stop_at_main: bool = True,
        timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        if not gdb.binary:
            raise ValueError("No binary loaded. Call gdb_start with a binary first.")
        if args:
            argstr = args if isinstance(args, str) else " ".join(args)
            await gdb.send(f"-exec-arguments {argstr}")
        command = "-exec-run --start" if stop_at_main else "-exec-run"
        try:
            _result, stop = await gdb.execute(command, stop_timeout=timeout)
        except GdbError:
            if not stop_at_main:
                raise
            # --start needs a `main`; retry plainly for stripped/odd binaries.
            _result, stop = await gdb.execute("-exec-run", stop_timeout=timeout)
        return self._exec_result(gdb, stop)

    async def cont(
        self, *, session: str = "default", timeout: float = DEFAULT_STOP_TIMEOUT
    ) -> dict[str, Any]:
        gdb = self.get(session)
        _result, stop = await gdb.execute("-exec-continue", stop_timeout=timeout)
        return self._exec_result(gdb, stop)

    async def step(
        self,
        kind: str = "step",
        *,
        count: int = 1,
        session: str = "default",
        timeout: float = DEFAULT_STOP_TIMEOUT,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        if kind not in STEP_KINDS:
            raise ValueError(
                f"Unknown step kind {kind!r}. Use one of: {', '.join(sorted(STEP_KINDS))}"
            )
        command = STEP_KINDS[kind]
        stop: StopEvent | None = None
        for _ in range(max(1, int(count))):
            _result, stop = await gdb.execute(command, stop_timeout=timeout)
            if stop is None or stop.exited:
                break
        return self._exec_result(gdb, stop)

    async def interrupt(self, *, session: str = "default") -> dict[str, Any]:
        gdb = self.get(session)
        stop = await gdb.interrupt()
        return self._exec_result(gdb, stop)

    def _exec_result(self, gdb: GdbSession, stop: StopEvent | None) -> dict[str, Any]:
        out = describe_stop(stop, running=gdb.running)
        output = gdb.read_program_output()
        if output:
            out["program_output"] = truncate(output, 4000)
        return out

    # -- breakpoints --------------------------------------------------------

    async def set_breakpoint(
        self,
        location: str,
        *,
        session: str = "default",
        condition: str | None = None,
        temporary: bool = False,
        pending: bool = True,
        ignore_count: int | None = None,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        parts = ["-break-insert"]
        if temporary:
            parts.append("-t")
        if pending:
            parts.append("-f")
        if condition:
            parts.append(f'-c "{condition}"')
        if ignore_count:
            parts.append(f"-i {int(ignore_count)}")
        parts.append(location)
        result = await gdb.send(" ".join(parts))
        bkpt = result.payload.get("bkpt", {})
        return {"breakpoint": bkpt}

    async def set_watchpoint(
        self, expression: str, *, session: str = "default", kind: str = "write"
    ) -> dict[str, Any]:
        gdb = self.get(session)
        flag = {"write": "", "read": "-r", "access": "-a"}.get(kind)
        if flag is None:
            raise ValueError("kind must be one of: write, read, access")
        result = await gdb.send(f"-break-watch {flag} {expression}".replace("  ", " "))
        return {"watchpoint": result.payload}

    async def list_breakpoints(self, *, session: str = "default") -> dict[str, Any]:
        gdb = self.get(session)
        result = await gdb.send("-break-list")
        table = result.payload.get("BreakpointTable", {})
        return {"breakpoints": _as_list(table.get("body"))}

    async def delete_breakpoint(
        self, number: int | str, *, session: str = "default"
    ) -> dict[str, Any]:
        gdb = self.get(session)
        await gdb.send(f"-break-delete {number}")
        return {"deleted": number}

    async def enable_breakpoint(
        self, number: int | str, *, enabled: bool = True, session: str = "default"
    ) -> dict[str, Any]:
        gdb = self.get(session)
        verb = "-break-enable" if enabled else "-break-disable"
        await gdb.send(f"{verb} {number}")
        return {"breakpoint": number, "enabled": enabled}

    # -- inspection ---------------------------------------------------------

    async def backtrace(
        self,
        *,
        session: str = "default",
        limit: int = 30,
        full: bool = False,
        thread: int | None = None,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        thread_arg = f" --thread {thread}" if thread is not None else ""
        result = await gdb.send(f"-stack-list-frames{thread_arg} 0 {max(0, limit - 1)}")
        frames = _as_list(result.payload.get("stack"))
        out: dict[str, Any] = {"frames": frames, "frame_count": len(frames)}
        if full:
            locals_result = await gdb.send(
                f"-stack-list-variables{thread_arg} --simple-values"
            )
            out["variables"] = _as_list(locals_result.payload.get("variables"))
        return out

    async def select_frame(
        self, level: int, *, session: str = "default"
    ) -> dict[str, Any]:
        gdb = self.get(session)
        await gdb.send(f"-stack-select-frame {int(level)}")
        info = await gdb.send("-stack-info-frame")
        frame = info.payload.get("frame", {})
        variables = await gdb.send("-stack-list-variables --simple-values")
        out: dict[str, Any] = {
            "frame": frame,
            "variables": _as_list(variables.payload.get("variables")),
        }
        if isinstance(frame, dict):
            ctx = source_context(frame.get("fullname"), frame.get("line"))
            if ctx:
                out["source"] = ctx
        return out

    async def evaluate(
        self,
        expression: str,
        *,
        session: str = "default",
        allow_function_calls: bool = False,
    ) -> dict[str, Any]:
        """Evaluate an expression in the selected frame.

        Inferior function calls are blocked by default: on a process you have
        attached to in production, `print some_func()` really does run that
        code inside the live process.
        """
        gdb = self.get(session)
        escaped = expression.replace("\\", "\\\\").replace('"', '\\"')
        if allow_function_calls:
            await gdb.send("-gdb-set may-call-functions on", raise_on_error=False)
        try:
            result = await gdb.send(f'-data-evaluate-expression "{escaped}"')
        except GdbError as exc:
            if "may-call-functions" in str(exc) or "not allowed" in str(exc).lower():
                raise GdbError(
                    f"{exc} -- this expression calls a function in the inferior. "
                    "Re-run with allow_function_calls=true if that side effect "
                    "is acceptable on this target."
                ) from exc
            raise
        finally:
            if allow_function_calls:
                await gdb.send("-gdb-set may-call-functions off", raise_on_error=False)
        return {
            "expression": expression,
            "value": truncate(str(result.payload.get("value", "")), 8000),
        }

    async def list_threads(self, *, session: str = "default") -> dict[str, Any]:
        gdb = self.get(session)
        result = await gdb.send("-thread-info")
        return {
            "threads": _as_list(result.payload.get("threads")),
            "current_thread_id": result.payload.get("current-thread-id"),
        }

    async def select_thread(
        self, thread_id: int, *, session: str = "default"
    ) -> dict[str, Any]:
        gdb = self.get(session)
        await gdb.send(f"-thread-select {int(thread_id)}")
        return await self.status(session=session)

    async def registers(
        self, *, session: str = "default", names: list[str] | None = None
    ) -> dict[str, Any]:
        gdb = self.get(session)
        names_result = await gdb.send("-data-list-register-names")
        all_names = [n for n in _as_list(names_result.payload.get("register-names"))]
        values_result = await gdb.send("-data-list-register-values x")
        values = _as_list(values_result.payload.get("register-values"))

        table: dict[str, Any] = {}
        for entry in values:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("number", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(all_names) and all_names[idx]:
                table[all_names[idx]] = entry.get("value")
        if names:
            table = {k: v for k, v in table.items() if k in set(names)}
        return {"registers": table}

    async def read_memory(
        self,
        address: str,
        *,
        session: str = "default",
        count: int = 16,
        word_size: int = 8,
        per_row: int | None = None,
        as_char: bool = True,
    ) -> dict[str, Any]:
        """Dump a range of memory in hex, as `count` words of `word_size` bytes.

        `address` may be any expression GDB accepts: `0x7ffd1234`, `&buf`,
        `$sp`, `packet->payload`.
        """
        gdb = self.get(session)
        if word_size not in WORD_SIZES:
            raise ValueError(
                f"word_size must be one of {WORD_SIZES} "
                "(4 = word, 8 = giant word, as in GDB's x/ sizes)"
            )
        count = max(1, int(count))
        cols = int(per_row or _COLS_BY_WORD_SIZE.get(word_size, 4))
        rows = (count + cols - 1) // cols

        command = f"-data-read-memory {address} x {word_size} {rows} {cols}"
        if as_char:
            # GDB wants the literal replacement character here, not its ASCII
            # code: passing "46" makes every unprintable byte render as '4'.
            command += " ."
        result = await gdb.send(command)
        payload = result.payload

        parsed_rows = []
        for row in _as_list(payload.get("memory")):
            if isinstance(row, dict):
                parsed_rows.append(
                    {
                        "addr": row.get("addr"),
                        "data": _as_list(row.get("data")),
                        "ascii": row.get("ascii"),
                    }
                )

        out: dict[str, Any] = {
            "address": payload.get("addr", address),
            "requested": {
                "expression": address,
                "count": count,
                "word_size": word_size,
                "format": "hexadecimal",
            },
            "total_bytes": payload.get("total-bytes"),
            "rows": parsed_rows,
            "dump": truncate(render_rows(parsed_rows), 8000),
        }
        for key, name in (("next-page", "next_page"), ("prev-page", "prev_page")):
            if payload.get(key):
                out[name] = payload[key]
        return out

    async def list_globals(
        self,
        *,
        session: str = "default",
        pattern: str | None = None,
        include_values: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Enumerate global and file-static variables.

        `pattern` is a regular expression matched against the name; without one
        this can return thousands of symbols from linked libraries, so it is
        strongly recommended.
        """
        gdb = self.get(session)
        command = "-symbol-info-variables"
        if pattern:
            command += f' --name "{pattern}"'
        command += f" --max-results {int(limit)}"

        try:
            result = await gdb.send(command, timeout=120)
        except GdbError:
            # -symbol-info-variables needs GDB >= 10.1; fall back to the CLI.
            text = await gdb.console(f"info variables {pattern or ''}".strip(), timeout=120)
            return {
                "source": "info variables (text; GDB < 10.1)",
                "pattern": pattern,
                "listing": truncate(text, 12000),
            }

        found: list[dict[str, Any]] = []
        symbols = result.payload.get("symbols", {})
        for group_key in ("debug", "nondebug"):
            for group in _as_list(symbols.get(group_key)):
                if not isinstance(group, dict):
                    continue
                filename = group.get("filename") or group.get("fullname")
                for symbol in _as_list(group.get("symbols")):
                    if not isinstance(symbol, dict):
                        continue
                    entry = {
                        "name": symbol.get("name"),
                        "type": symbol.get("type"),
                        "file": filename,
                    }
                    if symbol.get("line"):
                        entry["line"] = symbol["line"]
                    found.append(entry)

        truncated = len(found) > limit
        found = found[:limit]

        if include_values:
            for entry in found:
                name = entry.get("name")
                if not name:
                    continue
                try:
                    value = await self.evaluate(name, session=session)
                    entry["value"] = value["value"]
                except (GdbError, ValueError) as exc:
                    entry["value_error"] = str(exc)

        out: dict[str, Any] = {
            "pattern": pattern,
            "count": len(found),
            "globals": found,
        }
        if truncated:
            out["note"] = (
                f"Result capped at {limit}. Narrow it with a `pattern` regex."
            )
        if not found:
            out["note"] = (
                "No globals matched. Note that statics are scoped to their file "
                "-- read one with gdb_eval(\"'file.c'::name\")."
            )
        return out

    async def disassemble(
        self,
        *,
        session: str = "default",
        location: str | None = None,
        count: int = 20,
        with_source: bool = True,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        mode = 5 if with_source else 0
        # GDB requires an explicit location: the bare `-data-disassemble -- N`
        # form is rejected outright ("Usage: -f | -s/-e | -a"). `-a` covers the
        # function containing an address, which is what a caller means by
        # "disassemble here".
        target = location or "$pc"
        try:
            result = await gdb.send(f"-data-disassemble -a {target} -- {mode}")
        except GdbError:
            # mode 5 (source + opcodes) needs GDB >= 7.11; retry plainly.
            result = await gdb.send(f"-data-disassemble -a {target} -- 0")
        return {
            "location": target,
            "instructions": _as_list(result.payload.get("asm_insns"))[:count],
        }

    async def list_source(
        self,
        *,
        session: str = "default",
        location: str | None = None,
        lines: int = 20,
    ) -> dict[str, Any]:
        gdb = self.get(session)
        if location:
            output = await gdb.console(f"list {location}")
        else:
            frame = (await gdb.send("-stack-info-frame")).payload.get("frame", {})
            ctx = source_context(
                frame.get("fullname"),
                frame.get("line"),
                before=lines // 2,
                after=lines // 2,
            )
            if ctx:
                return {"file": frame.get("fullname"), "source": ctx}
            output = await gdb.console("list")
        return {"source_text": truncate(output, 8000)}

    async def program_output(self, *, session: str = "default") -> dict[str, Any]:
        gdb = self.get(session)
        text = truncate(gdb.read_program_output(), 8000)
        out: dict[str, Any] = {"output": text}
        if not text:
            # Reads drain the buffer, and every stop folds pending output into
            # its own result. Empty means "nothing new", not "printed nothing".
            out["note"] = (
                "No output since it was last reported. Output is delivered "
                "once: anything the program printed earlier was included in "
                "the preceding run, step or continue result."
            )
        return out

    async def raw(
        self, command: str, *, session: str = "default", timeout: float = 60.0
    ) -> dict[str, Any]:
        """Escape hatch: run an arbitrary GDB CLI command."""
        gdb = self.get(session)
        output = await gdb.console(command, timeout=timeout)
        return {"command": command, "output": truncate(output)}

    async def shutdown_all(self) -> None:
        for name in list(self.sessions):
            with contextlib.suppress(Exception):
                await self.stop(session=name, kill=False)
