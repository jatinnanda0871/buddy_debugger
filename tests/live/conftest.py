"""Fixtures for tests that talk to a real debugger.

Everything here skips cleanly when its dependency is absent, so a plain
`pytest -q` on a machine without gdb (or without VS Code) stays green and
fast. Nothing in this directory uses a test double.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _helpers import CXX_CANDIDATES, find_cxx

HERE = Path(__file__).parent


@pytest.fixture(scope="session")
def cxx() -> str:
    compiler = find_cxx()
    if compiler is None:
        pytest.skip(
            "no clang++ on PATH (tried " + ", ".join(CXX_CANDIDATES) + ")"
        )
    return compiler


@pytest.fixture(scope="session")
def gdb_path() -> str:
    found = shutil.which("gdb")
    if found is None:
        pytest.skip("gdb is not installed")
    return found


@pytest.fixture(scope="session")
def target_binary(cxx: str, gdb_path: str, tmp_path_factory) -> str:
    """Compile the C++ target once per run, with debug info and no inlining."""
    out = tmp_path_factory.mktemp("live") / "target"
    result = subprocess.run(
        [
            cxx,
            "-g",
            "-O0",
            "-std=c++17",
            "-fno-omit-frame-pointer",
            str(HERE / "target.cpp"),
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not compile the target:\n{result.stderr[:600]}")
    return str(out)


@pytest.fixture
async def debugger(gdb_path: str):
    """A Debugger whose sessions are always torn down, even on failure.

    Depends on gdb_path so that every test using it skips on a machine without
    gdb, rather than only the ones that also need a compiled target.
    """
    from gdb_mcp.tools import Debugger

    dbg = Debugger()
    try:
        yield dbg
    finally:
        await dbg.shutdown_all()


@pytest.fixture
async def stopped_in_parse_header(debugger, target_binary):
    """The common starting point: halted inside parse_header, frame 0."""
    await debugger.start(binary=target_binary)
    await debugger.set_breakpoint("parse_header")
    stop = await debugger.run(stop_at_main=False, timeout=60)
    assert stop["state"] == "stopped", f"did not reach the breakpoint: {stop}"
    return debugger


# -- the VS Code bridge -------------------------------------------------------


@pytest.fixture
def live_bridge():
    """A client bound to a real VS Code window running the bridge extension.

    Gated on an explicit opt-in as well as discovery: these tests start and
    stop debug sessions in the developer's editor, which is far too intrusive
    to happen merely because VS Code happened to be open.
    """
    if os.environ.get("GDB_MCP_LIVE_BRIDGE") != "1":
        pytest.skip("set GDB_MCP_LIVE_BRIDGE=1 to drive the real VS Code bridge")

    from gdb_mcp.vscode_bridge import BridgeError, VSCodeDebugger, discover

    try:
        discover()
    except BridgeError as exc:
        pytest.skip(f"no VS Code bridge found: {exc}")
    return VSCodeDebugger()


@pytest.fixture
def python_target() -> str:
    return str(HERE / "target.py")
