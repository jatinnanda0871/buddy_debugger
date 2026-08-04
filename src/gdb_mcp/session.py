"""Async driver for a `gdb --interpreter=mi` subprocess.

Responsibilities:

* spawn GDB and negotiate a sane, non-interactive configuration
* correlate commands with their result records via MI tokens
* surface execution stops (`*stopped`) as awaitable events, so a caller can
  issue `-exec-continue` and block until the inferior actually halts
* keep the inferior's own stdout/stderr off the MI stream by giving it a
  dedicated pty
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import mi
from .mi import MIRecord

IS_POSIX = os.name == "posix"

#: Settings applied to every session.  `pagination off` is not optional: with
#: it on, GDB blocks forever on "---Type <return> to continue---".
_STARTUP_COMMANDS = (
    "-gdb-set confirm off",
    "-gdb-set pagination off",
    "-gdb-set height 0",
    "-gdb-set width 0",
    "-gdb-set print pretty on",
    "-gdb-set backtrace past-main on",
    # All integer output in hex, so evaluated values, locals and memory dumps
    # agree with each other rather than mixing radices.
    "-gdb-set output-radix 16",
    # Safety default: block inferior function calls during expression
    # evaluation.  gdb_eval() lifts this per-call when explicitly asked.
    "-gdb-set may-call-functions off",
    # Needed for -exec-interrupt to work on a running inferior.
    "-gdb-set mi-async on",
    "-enable-pretty-printing",
)


class GdbError(RuntimeError):
    """A GDB command returned `^error`."""

    def __init__(self, message: str, command: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.command = command


class GdbStartupError(RuntimeError):
    """GDB could not be started or did not become ready."""


@dataclass
class CommandResult:
    """The outcome of a single MI command."""

    record: MIRecord
    #: `~` console output emitted while the command was in flight
    console: str = ""
    #: `&` log output emitted while the command was in flight
    log: str = ""

    @property
    def payload(self) -> dict[str, Any]:
        return self.record.payload

    @property
    def message(self) -> str | None:
        return self.record.message


@dataclass
class StopEvent:
    """A parsed `*stopped` async record."""

    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    console: str = ""

    @property
    def exited(self) -> bool:
        return self.reason.startswith("exited")


class GdbSession:
    """One GDB subprocess."""

    def __init__(
        self,
        gdb_path: str = "gdb",
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        mi_version: str = "mi3",
        use_init_file: bool = False,
        program_output_limit: int = 256 * 1024,
    ) -> None:
        self.gdb_path = gdb_path
        self.cwd = cwd
        self.env = env
        self.mi_version = mi_version
        self.use_init_file = use_init_file

        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._token = 0
        self._pending: dict[int, asyncio.Future[MIRecord]] = {}
        self._cmd_lock = asyncio.Lock()
        self._stream: list[MIRecord] = []
        self._ready = asyncio.Event()
        self._exited = asyncio.Event()

        self._stops: asyncio.Queue[StopEvent] = asyncio.Queue()
        self.running = False
        self.last_stop: StopEvent | None = None

        # Inferior tty
        self._pty_master: int | None = None
        self._pty_slave: int | None = None
        self.inferior_tty: str | None = None
        self._program_output: deque[bytes] = deque()
        self._program_output_len = 0
        self._program_output_limit = program_output_limit

        # Session bookkeeping surfaced to the caller.
        self.binary: str | None = None
        self.attached_pid: int | None = None
        self.core_file: str | None = None

    # -- lifecycle ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if shutil.which(self.gdb_path) is None and not os.path.isabs(self.gdb_path):
            raise GdbStartupError(
                f"{self.gdb_path!r} not found on PATH. Install gdb or pass gdb_path."
            )
        # mi3 needs GDB >= 8.1; fall back so older workplace toolchains work.
        versions = [self.mi_version] + (
            ["mi2"] if self.mi_version != "mi2" else []
        )
        last: Exception | None = None
        for version in versions:
            try:
                await self._spawn(version)
                self.mi_version = version
                return
            except GdbStartupError as exc:
                last = exc
                await self._teardown_process()
        raise last or GdbStartupError("could not start gdb")

    async def _spawn(self, mi_version: str) -> None:
        argv = [self.gdb_path, f"--interpreter={mi_version}", "-q"]
        if not self.use_init_file:
            argv.append("-nx")

        self._open_pty()
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
            )
        except OSError as exc:
            # e.g. an absolute gdb_path that does not exist, or is not
            # executable; without this the caller sees a raw FileNotFoundError.
            raise GdbStartupError(f"could not launch {self.gdb_path!r}: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError as exc:
            raise GdbStartupError(
                f"gdb did not produce an MI prompt (interpreter={mi_version})"
            ) from exc
        if not self.alive:
            raise GdbStartupError(
                f"gdb exited immediately (interpreter={mi_version})"
            )

        for command in _STARTUP_COMMANDS:
            # Older GDBs reject a few of these; none are fatal on their own.
            with contextlib.suppress(GdbError, asyncio.TimeoutError):
                await self.send(command, timeout=10)

        if self.inferior_tty:
            with contextlib.suppress(GdbError, asyncio.TimeoutError):
                await self.send(
                    f"-gdb-set inferior-tty {self.inferior_tty}", timeout=10
                )

    def _open_pty(self) -> None:
        """Give the inferior its own tty so its output never corrupts MI."""
        if not IS_POSIX:
            return
        import pty

        try:
            master, slave = pty.openpty()
        except OSError:
            return
        self._pty_master, self._pty_slave = master, slave
        self.inferior_tty = os.ttyname(slave)
        os.set_blocking(master, False)
        loop = asyncio.get_running_loop()
        try:
            loop.add_reader(master, self._drain_pty)
        except (NotImplementedError, OSError):
            # Event loop can't watch this fd; fall back to GDB's own tty.
            self._close_pty()
            self.inferior_tty = None

    def _drain_pty(self) -> None:
        assert self._pty_master is not None
        try:
            data = os.read(self._pty_master, 65536)
        except BlockingIOError:
            return
        except OSError:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._pty_master)
            return
        if not data:
            return
        self._program_output.append(data)
        self._program_output_len += len(data)
        while self._program_output_len > self._program_output_limit:
            self._program_output_len -= len(self._program_output.popleft())

    def _close_pty(self) -> None:
        if self._pty_master is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._pty_master)
            with contextlib.suppress(OSError):
                os.close(self._pty_master)
        if self._pty_slave is not None:
            with contextlib.suppress(OSError):
                os.close(self._pty_slave)
        self._pty_master = self._pty_slave = None

    def read_program_output(self, *, clear: bool = True) -> str:
        data = b"".join(self._program_output)
        if clear:
            self._program_output.clear()
            self._program_output_len = 0
        return data.decode("utf-8", "replace")

    async def close(self, *, kill: bool = True, timeout: float = 5.0) -> None:
        """Shut down. When attached to a live process, `kill=False` detaches."""
        if self.alive:
            with contextlib.suppress(Exception):
                if self.running:
                    await self._raw_write("-exec-interrupt --all")
                    await asyncio.sleep(0.2)
            with contextlib.suppress(Exception):
                if self.attached_pid is not None and not kill:
                    await self.send("-target-detach", timeout=timeout)
                elif self.binary or self.attached_pid is not None:
                    await self.send("-target-kill", timeout=timeout)
            with contextlib.suppress(Exception):
                await self._raw_write("-gdb-exit")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=timeout)  # type: ignore[union-attr]
        await self._teardown_process()

    async def _teardown_process(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(GdbStartupError("gdb session closed"))
        self._pending.clear()
        self._close_pty()
        self._exited.set()

    # -- reader -------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Pathologically long line; skip it rather than dying.
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            record = mi.parse_line(line)
            if record is None:
                continue
            self._dispatch(record)
        self.running = False
        self._exited.set()
        self._ready.set()

    def _dispatch(self, record: MIRecord) -> None:
        kind = record.kind

        if kind == mi.PROMPT:
            self._ready.set()
            return

        if kind in mi.STREAM_KINDS:
            self._stream.append(record)
            if kind == mi.TARGET and record.text:
                # Some targets route inferior output through MI instead of the tty.
                data = record.text.encode("utf-8", "replace")
                self._program_output.append(data)
                self._program_output_len += len(data)
            return

        if kind == mi.RESULT:
            if record.message == "running":
                self.running = True
            elif record.message in ("done", "error", "exit", "connected"):
                pass
            if record.token is not None:
                fut = self._pending.pop(record.token, None)
                if fut is not None and not fut.done():
                    fut.set_result(record)
            return

        if kind == mi.EXEC:
            if record.message == "running":
                self.running = True
            elif record.message == "stopped":
                self.running = False
                payload = record.payload
                reason = payload.get("reason") or "unknown"
                if not isinstance(reason, str):
                    reason = str(reason)
                event = StopEvent(
                    reason=reason,
                    payload=payload,
                    console=self._peek_console(),
                )
                self.last_stop = event
                self._stops.put_nowait(event)
            return

        if kind == mi.NOTIFY:
            if record.message in ("thread-group-exited",):
                self.running = False
            return

    def _peek_console(self) -> str:
        return "".join(r.text or "" for r in self._stream if r.kind == mi.CONSOLE)

    def _take_stream(self) -> tuple[str, str]:
        console = "".join(r.text or "" for r in self._stream if r.kind == mi.CONSOLE)
        log = "".join(r.text or "" for r in self._stream if r.kind == mi.LOG)
        self._stream.clear()
        return console, log

    # -- commands -----------------------------------------------------------

    async def _raw_write(self, command: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise GdbStartupError("gdb is not running")
        self.proc.stdin.write((command + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def send(
        self,
        command: str,
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> CommandResult:
        """Send one MI command and wait for its result record."""
        if not self.alive:
            raise GdbStartupError("gdb is not running")

        async with self._cmd_lock:
            self._token += 1
            token = self._token
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[MIRecord] = loop.create_future()
            self._pending[token] = fut
            self._stream.clear()
            try:
                await self._raw_write(f"{token}{command}")
                record = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(token, None)
                raise asyncio.TimeoutError(
                    f"gdb did not answer {command!r} within {timeout}s"
                ) from None
            finally:
                self._pending.pop(token, None)
            console, log = self._take_stream()

        result = CommandResult(record=record, console=console, log=log)
        if raise_on_error and record.is_error:
            raise GdbError(record.error_message, command)
        return result

    async def console(self, command: str, *, timeout: float = 30.0) -> str:
        """Run a plain CLI command and return its console output."""
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        result = await self.send(f'-interpreter-exec console "{escaped}"', timeout=timeout)
        return result.console

    def drain_stops(self) -> None:
        """Discard buffered stop events so the next wait sees only new ones."""
        while not self._stops.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._stops.get_nowait()

    async def wait_for_stop(self, timeout: float) -> StopEvent | None:
        try:
            return await asyncio.wait_for(self._stops.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def execute(
        self,
        command: str,
        *,
        stop_timeout: float = 30.0,
        command_timeout: float = 20.0,
    ) -> tuple[CommandResult, StopEvent | None]:
        """Run an execution command and wait for the inferior to halt.

        Returns `(result, stop)`. `stop` is None when the inferior is still
        running after `stop_timeout` -- the caller should report that and offer
        `interrupt`, not pretend the program stopped.
        """
        self.drain_stops()
        result = await self.send(command, timeout=command_timeout)
        if result.message not in ("running", "done"):
            return result, None
        stop = await self.wait_for_stop(stop_timeout)
        return result, stop

    async def interrupt(self, *, timeout: float = 10.0) -> StopEvent | None:
        """Halt a running inferior (SIGINT via MI, with a POSIX fallback)."""
        if not self.running:
            return self.last_stop
        self.drain_stops()
        try:
            await self.send("-exec-interrupt --all", timeout=timeout)
        except GdbError:
            if IS_POSIX and self.attached_pid:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(self.attached_pid, signal.SIGINT)
        return await self.wait_for_stop(timeout)
