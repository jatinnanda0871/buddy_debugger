"""The BrokenResourceError family: a client that dies mid-call.

If the MCP client's own request timeout is shorter than a slow tool call
(core-dump loads and vsc_launch can legitimately take up to two minutes), it
may kill or reconnect the subprocess while the call is still running. The
server then tries to answer into a transport that is already gone, which
anyio surfaces as BrokenResourceError -- sometimes bundled with other
in-flight failures inside an ExceptionGroup/BaseExceptionGroup. `main()` must
recognize that family and exit cleanly with a clear log line instead of an
unhandled traceback.
"""

from __future__ import annotations

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from gdb_mcp import server as server_module
from gdb_mcp.server import _is_clean_shutdown, _leaf_exceptions, build_server
from gdb_mcp.tools import Debugger
from gdb_mcp.vscode_bridge import VSCodeDebugger

# -- _leaf_exceptions / _is_clean_shutdown -------------------------------


class _FakeGroup(Exception):
    """Stands in for (Base)ExceptionGroup without caring which Python version."""

    def __init__(self, *exceptions: BaseException) -> None:
        super().__init__(exceptions)
        self.exceptions = exceptions


def test_leaf_exceptions_passes_through_a_plain_exception():
    exc = RuntimeError("boom")
    assert _leaf_exceptions(exc) == [exc]


def test_leaf_exceptions_flattens_nested_groups():
    a = RuntimeError("a")
    b = KeyboardInterrupt()
    c = ValueError("c")
    group = _FakeGroup(a, _FakeGroup(b, c))
    assert _leaf_exceptions(group) == [a, b, c]


def test_plain_keyboard_interrupt_is_a_clean_shutdown():
    assert _is_clean_shutdown(KeyboardInterrupt()) is True


def test_keyboard_interrupt_wrapped_in_a_group_is_still_clean():
    assert _is_clean_shutdown(_FakeGroup(KeyboardInterrupt())) is True


def test_broken_resource_error_is_not_a_clean_shutdown():
    assert _is_clean_shutdown(RuntimeError("broken pipe")) is False


def test_a_group_mixing_ctrl_c_with_a_real_error_is_not_clean():
    group = _FakeGroup(KeyboardInterrupt(), RuntimeError("broken pipe"))
    assert _is_clean_shutdown(group) is False


# -- the actual disconnect-mid-call race ----------------------------------


async def test_client_disconnecting_mid_call_surfaces_a_dirty_shutdown(monkeypatch):
    """Reproduces the production race: the client's receive side vanishes
    while a tool call is still being dispatched, before the server gets a
    chance to write the response.
    """
    entered = anyio.Event()
    release = anyio.Event()

    async def slow_dispatch(dbg, name, session, args):
        entered.set()
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(server_module, "_dispatch_gdb", slow_dispatch)
    srv = build_server(Debugger(gdb_path="gdb-does-not-exist"), VSCodeDebugger())

    server_errors: list[BaseException] = []
    server_done = anyio.Event()

    async def run_server(server_read, server_write):
        try:
            await srv.run(
                server_read, server_write, srv.create_initialization_options()
            )
        except BaseException as exc:  # noqa: BLE001 -- capturing for inspection
            server_errors.append(exc)
        finally:
            server_done.set()

    async def client_task(client_read, client_write):
        # A single self-contained `async with` so __aenter__/__aexit__ stay
        # correctly paired even under cancellation -- splitting them across
        # manual calls sandwiched between other scopes breaks anyio's LIFO
        # cancel-scope nesting.
        async with ClientSession(read_stream=client_read, write_stream=client_write) as client:
            await client.initialize()
            try:
                await client.call_tool("gdb_session", {"operation": "start"})
            except BaseException:  # noqa: BLE001 -- the point is it doesn't hang
                pass

    with anyio.fail_after(10):
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            client_read, client_write = client_streams
            server_read, server_write = server_streams

            async with anyio.create_task_group() as tg:
                tg.start_soon(run_server, server_read, server_write)
                tg.start_soon(client_task, client_read, client_write)

                await entered.wait()

                # The client process dies mid-call: both directions of its
                # channel disappear before the server's answer arrives, the
                # same as an OS killing every pipe of a subprocess at once.
                await client_read.aclose()
                await client_write.aclose()
                release.set()

                await server_done.wait()
                tg.cancel_scope.cancel()

    # What the SDK does with this varies by version: 1.10.x (what's actually
    # pinned in production) lets the late response write fail loudly as a
    # (possibly doubly-nested) ExceptionGroup wrapping BrokenResourceError /
    # ClosedResourceError -- confirmed by running this exact scenario against
    # mcp==1.10.0. Newer SDKs cancel the in-flight handler before it can write
    # and swallow the race, so server.run() just returns. Either way it must
    # not hang, and whatever it does raise must not be misclassified as a
    # clean Ctrl-C shutdown.
    if server_errors:
        assert not _is_clean_shutdown(server_errors[0]), (
            "a broken write mid-call is a real failure -- main() must log it, "
            "not treat it as a clean Ctrl-C shutdown"
        )
