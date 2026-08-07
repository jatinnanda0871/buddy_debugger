"""The vsc_* backend against a real **cppdbg** session driving real GDB.

This is the configuration the project actually targets: C++, cppdbg, gdb. The
debugpy tests next door cover the plain-DAP paths, but they can only assert
that the GDB-backed tools *refuse*. Everything here is the other half --
vsc_exec, vsc_globals, vsc_memory and vsc_disassemble doing real work.

Requires, on top of GDB_MCP_LIVE_BRIDGE=1:

  * the C/C++ extension (ms-vscode.cpptools) in the VS Code running the bridge
  * a gdb and a compiled target reachable by that VS Code

On Windows the target is built and debugged inside WSL, with cppdbg reaching it
over pipeTransport; on Linux everything is local. Set GDB_MCP_CPP_PROGRAM to
point at a prebuilt binary and skip the automatic build.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _helpers import find_cxx

pytestmark = pytest.mark.live

HERE = Path(__file__).parent
SOURCE = HERE / "target.cpp"

#: `if ((int)out->length > limit)` -- reached, with locals already bound.
BREAKPOINT_LINE = 27

WSL_DISTRO = os.environ.get("GDB_MCP_WSL_DISTRO", "Ubuntu-24.04")


def _wsl_available() -> bool:
    if sys.platform != "win32" or not shutil.which("wsl"):
        return False
    probe = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "-e", "sh", "-c", "command -v gdb clang++-14"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and "gdb" in probe.stdout


@pytest.fixture(scope="session")
def cpp_program(tmp_path_factory) -> str:
    """Path to a debuggable C++ binary, as the debug adapter will see it."""
    preset = os.environ.get("GDB_MCP_CPP_PROGRAM")
    if preset:
        return preset

    if sys.platform == "win32":
        if not _wsl_available():
            pytest.skip(
                f"needs WSL ({WSL_DISTRO}) with gdb and clang++-14, or "
                "GDB_MCP_CPP_PROGRAM pointing at a prebuilt binary"
            )
        out = "/tmp/gdb-mcp-live/target"
        build = subprocess.run(
            [
                "wsl", "-d", WSL_DISTRO, "-e", "sh", "-c",
                "mkdir -p /tmp/gdb-mcp-live && clang++-14 -g -O0 -std=c++17 "
                "-fno-omit-frame-pointer "
                "$(wslpath -a '" + str(SOURCE) + "') -o " + out,
            ],
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            pytest.skip(f"could not build in WSL:\n{build.stderr[:400]}")
        return out

    compiler = find_cxx()
    if compiler is None or shutil.which("gdb") is None:
        pytest.skip("needs gdb and clang++ to build the C++ target")
    out = str(tmp_path_factory.mktemp("cpp") / "target")
    build = subprocess.run(
        [compiler, "-g", "-O0", "-std=c++17", str(SOURCE), "-o", out],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        pytest.skip(f"could not compile: {build.stderr[:400]}")
    return out


@pytest.fixture(scope="session")
def cpp_config(cpp_program: str) -> dict:
    config = {
        "name": "gdb-mcp cpp live test",
        "type": "cppdbg",
        "request": "launch",
        "program": cpp_program,
        "args": [],
        "cwd": str(Path(cpp_program).parent),
        "stopAtEntry": False,
        "externalConsole": False,
        "MIMode": "gdb",
    }
    if sys.platform == "win32":
        repo = HERE.parent.parent
        config["cwd"] = "/tmp/gdb-mcp-live"
        config["pipeTransport"] = {
            # `sh -c` is required: cppdbg passes the debugger command as a
            # single argument ("/usr/bin/gdb --interpreter=mi"), so without a
            # shell to split it, WSL execs that whole string as a filename.
            "pipeProgram": "C:\\Windows\\System32\\wsl.exe",
            "pipeArgs": ["-d", WSL_DISTRO, "--", "sh", "-c"],
            "pipeCwd": str(repo),
            "debuggerPath": "/usr/bin/gdb",
        }
        drive = str(repo)[0].lower()
        config["sourceFileMap"] = {f"/mnt/{drive}": f"{drive}:\\"}
    return config


@pytest.fixture
async def cpp_paused(live_bridge, cpp_config):
    """A cppdbg session halted inside parse_header."""
    await live_bridge.set_breakpoints([], action="clear")
    await live_bridge.set_breakpoints(
        [{"file": str(SOURCE), "line": BREAKPOINT_LINE}]
    )
    launched = await live_bridge.launch(
        config=cpp_config, wait_for_stop=True, timeout=240
    )
    stop = launched.get("stop", {})
    if stop.get("state") != "stopped":
        await live_bridge.terminate()
        pytest.skip(f"cppdbg did not reach the breakpoint: {stop}")
    try:
        yield live_bridge, stop
    finally:
        await live_bridge.set_breakpoints([], action="clear")
        try:
            await live_bridge.terminate()
        except Exception:  # noqa: BLE001 - teardown must not mask failures
            pass


# -- the session --------------------------------------------------------------


async def test_cppdbg_stops_at_the_breakpoint(cpp_paused):
    _bridge, stop = cpp_paused
    assert stop["reason"] == "breakpoint"
    # C++ frames carry the full signature, not the bare name.
    assert stop["frame"]["name"].startswith("parse_header")
    assert stop["frame"]["line"] == BREAKPOINT_LINE


async def test_source_is_mapped_back_to_the_local_file(cpp_paused):
    """Without sourceFileMap the path would be the debug host's, unreadable here."""
    _bridge, stop = cpp_paused
    assert stop["source"], "no source context"
    assert any(line["current"] for line in stop["source"])


async def test_cpp_call_chain(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.backtrace(limit=10)
    names = [frame["name"] for frame in result["frames"]]
    assert any(n.startswith("parse_header") for n in names)
    assert any(n.startswith("handle_packet") for n in names)
    assert any(n.startswith("main") for n in names)


async def test_cpp_locals_and_struct_expansion(cpp_paused):
    bridge, _stop = cpp_paused
    frames = (await bridge.backtrace(limit=1))["frames"]
    frame = await bridge.frame(frames[0]["id"], expand_depth=1)
    names = {
        var["name"] for scope in frame["scopes"] for var in scope.get("variables", [])
    }
    assert {"raw", "limit", "out"} & names, names


# -- the GDB-backed tools, doing real work ------------------------------------


async def test_exec_reaches_real_gdb(cpp_paused):
    """debugpy can only prove this refuses; here it must actually work."""
    bridge, _stop = cpp_paused
    result = await bridge.exec_gdb("info registers rip")
    assert "rip" in result["output"]
    assert "0x" in result["output"]


async def test_exec_can_run_an_arbitrary_gdb_command(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.exec_gdb("info frame")
    assert result["output"].strip(), "no output from `info frame`"


async def test_globals_are_enumerated_through_exec(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.list_globals(pattern="^g_")
    names = [entry["name"] for entry in result["globals"]]
    assert "g_connection_count" in names, names[:10]
    assert "g_table" in names, names[:10]


async def test_program_globals_outrank_library_noise(cpp_paused):
    """`g_` is an unanchored regex, so glibc symbols match inside their names."""
    bridge, _stop = cpp_paused
    result = await bridge.list_globals(pattern="g_")
    names = [entry["name"] for entry in result["globals"]]
    ours = {"g_connection_count", "g_hostname", "g_table"}
    assert ours & set(names), "our globals were not found at all"
    # entries carrying a source file (debug info) must be sorted to the front
    first_ten = names[:10]
    assert ours & set(first_ten), (
        f"our globals were buried under library symbols: {first_ten}"
    )


async def test_unanchored_pattern_warns_about_anchoring(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.list_globals(pattern="g_")
    if result["count"] > 25:
        assert "anchor" in result.get("note", "").lower()


async def test_memory_dump_over_cppdbg(cpp_paused):
    """Resolves `&g_table` to an address, then reads and renders it."""
    bridge, _stop = cpp_paused
    result = await bridge.read_memory("&g_table", count=4, word_size=8)
    words = [word for row in result["rows"] for word in row["data"]]
    assert words[0] == "0x1111111111111111", words
    assert words[3] == "0x4444444444444444", words


async def test_memory_dump_ascii_gutter_over_cppdbg(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.read_memory("&g_hostname", count=4, word_size=4)
    assert "prod-db-01" in result["dump"], result["dump"]


async def test_disassembly_over_cppdbg(cpp_paused):
    bridge, _stop = cpp_paused
    frames = (await bridge.backtrace(limit=1))["frames"]
    pc = frames[0].get("pc")
    assert pc, "cppdbg did not report an instruction pointer"
    result = await bridge.disassemble(pc, count=3)
    assert len(result["instructions"]) == 3
    assert result["instructions"][0]["address"]


async def test_evaluate_a_scalar_over_cppdbg(cpp_paused):
    bridge, _stop = cpp_paused
    # Hex, matching the gdb_* backend: 4096 == 0x1000.
    assert (await bridge.evaluate("limit"))["value"] == "0x1000"


async def test_evaluate_expands_aggregates(cpp_paused):
    """cppdbg renders char[32] as the bare summary "[32]"; contents are children."""
    bridge, _stop = cpp_paused
    result = await bridge.evaluate("g_hostname")
    rendered = result["value"] + str(result.get("children", ""))
    assert "prod" in rendered or result.get("children"), result


async def test_evaluate_expands_a_struct(cpp_paused):
    bridge, _stop = cpp_paused
    result = await bridge.evaluate("*out")
    names = {child["name"] for child in result.get("children", [])}
    assert {"length", "magic"} & names, result


# -- execution ----------------------------------------------------------------


async def test_stepping_a_cpp_program(cpp_paused):
    bridge, stop = cpp_paused
    result = await bridge.resume("next", timeout=60)
    assert result["state"] == "stopped"
    assert result["frame"]["line"] != stop["frame"]["line"]


async def test_continue_to_exit_captures_stdout(cpp_paused):
    bridge, _stop = cpp_paused
    await bridge.set_breakpoints([], action="clear")
    result = await bridge.resume("continue", timeout=90)
    assert result["state"] in ("exited", "terminated")
    output = result.get("program_output", "")
    assert "total=55" in output, repr(output[:200])


# -- hex parity with the gdb_* backend ----------------------------------------


async def test_integers_evaluate_in_hex_like_the_gdb_backend(cpp_paused):
    """gdb_eval reports 0x11 for this; vsc_eval must not report 17."""
    bridge, _stop = cpp_paused
    result = await bridge.evaluate("g_connection_count")
    assert result["value"].startswith("0x"), result
    assert int(result["value"], 16) == 17


async def test_hex_applies_to_frame_variables_too(cpp_paused):
    bridge, _stop = cpp_paused
    frames = (await bridge.backtrace(limit=1))["frames"]
    frame = await bridge.frame(frames[0]["id"], expand_depth=0)
    values = {
        var["name"]: var["value"]
        for scope in frame["scopes"]
        for var in scope.get("variables", [])
    }
    assert values.get("limit", "").startswith("0x"), values
