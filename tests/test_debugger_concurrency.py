"""Concurrent gdb_start for the same session name must not leak a session.

Debugger.start() reads the session dict, then `await`s a real subprocess
spawn before writing back to it. Two concurrent starts for the same name can
both pass the "not already running" read before either finishes the spawn --
without a lock serializing that window, the second registration silently
overwrites the first, leaking an orphaned, never-tracked gdb subprocess that
nothing ever stops.
"""

import asyncio
import os
import sys

from gdb_mcp import tools as tools_module
from gdb_mcp.session import GdbSession
from gdb_mcp.tools import Debugger

FAKE = os.path.join(os.path.dirname(__file__), "fake_gdb.py")


class RacyFakeGdbSession(GdbSession):
    """Launches the fake MI process, with a controllable pause right before
    the spawn -- the exact window `Debugger._start_locked` must serialize."""

    entered: asyncio.Event
    release: asyncio.Event
    spawn_count = 0

    def __init__(self, gdb_path="gdb", **kwargs):
        kwargs.pop("cwd", None)
        super().__init__(sys.executable, **kwargs)

    async def _spawn(self, mi_version):  # type: ignore[override]
        type(self).spawn_count += 1
        self.entered.set()
        await self.release.wait()
        argv = [sys.executable, FAKE, f"--interpreter={mi_version}", "-q", "-nx"]
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        for command in ("-gdb-set confirm off", "-gdb-set pagination off"):
            await self.send(command, timeout=10)


async def test_concurrent_start_for_the_same_session_does_not_leak_a_session(
    monkeypatch,
):
    entered_a = asyncio.Event()
    entered_b = asyncio.Event()
    release = asyncio.Event()
    RacyFakeGdbSession.spawn_count = 0

    # Debugger._start_locked constructs `GdbSession(...)` itself, so wire the
    # per-call entered/release events through a small factory rather than
    # patching attributes onto an instance we don't control the creation of.
    calls = iter([entered_a, entered_b])

    def factory(*args, **kwargs):
        session = RacyFakeGdbSession(*args, **kwargs)
        session.entered = next(calls)
        session.release = release
        return session

    monkeypatch.setattr(tools_module, "GdbSession", factory)

    dbg = Debugger(gdb_path=sys.executable)
    task_a = asyncio.create_task(dbg.start(session="default"))
    task_b = asyncio.create_task(dbg.start(session="default"))

    # Give both a chance to reach the spawn-entry checkpoint before either is
    # allowed to finish -- this is the actual race window being tested. With
    # the lock in place, task_b never reaches it at all (it blocks on lock
    # acquisition and, once task_a has registered, fails its own "already
    # running" check before ever constructing a GdbSession).
    await asyncio.wait_for(entered_a.wait(), timeout=5)
    await asyncio.sleep(0.05)
    release.set()

    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert RacyFakeGdbSession.spawn_count == 1, (
        "the lock must stop a second concurrent start for the same session "
        "name from ever spawning a second gdb subprocess"
    )
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "already running" in str(failures[0])
    assert len(dbg.sessions) == 1

    await dbg.shutdown_all()
