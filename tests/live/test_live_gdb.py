"""The gdb_* backend against a real GDB and a real clang-built C++ binary.

Skips unless both gdb and clang++ are installed. These are the tests that
actually catch things: every defect found in this backend so far (the `aschar
46` gutter, the rejected `-data-disassemble` form, CRLF from the pty) passed
the fake-MI suite and only failed here.
"""

from __future__ import annotations

import os

import pytest

from gdb_mcp.tools import WORD_SIZES

pytestmark = pytest.mark.live


# -- session lifecycle --------------------------------------------------------


async def test_session_starts_and_reports_its_gdb(debugger, target_binary):
    info = await debugger.start(binary=target_binary)
    assert "GNU gdb" in info.get("gdb_version", "")
    assert info["binary"] == os.path.abspath(target_binary)


async def test_mi3_is_negotiated(debugger, target_binary):
    info = await debugger.start(binary=target_binary)
    assert info["mi_version"] in ("mi3", "mi2")


async def test_inferior_gets_its_own_pty(debugger, target_binary):
    """Without this the program's stdout corrupts the MI stream."""
    info = await debugger.start(binary=target_binary)
    assert info["inferior_tty"], "no pty allocated for the inferior"
    assert info["inferior_tty"].startswith("/dev/")


async def test_missing_binary_is_a_clear_error(debugger):
    await debugger.start()
    with pytest.raises(ValueError, match="Binary not found"):
        await debugger.load_binary("/nonexistent/binary")


# -- symbols ------------------------------------------------------------------


async def test_globals_are_enumerated_with_types(debugger, target_binary):
    await debugger.start(binary=target_binary)
    result = await debugger.list_globals(pattern="g_")
    by_name = {entry["name"]: entry for entry in result["globals"]}

    assert "g_connection_count" in by_name
    assert by_name["g_connection_count"]["type"] == "int"
    assert by_name["g_connection_count"]["file"].endswith("target.cpp")


async def test_file_static_globals_are_found(debugger, target_binary):
    """g_retries is `static`, so it is invisible outside its translation unit."""
    await debugger.start(binary=target_binary)
    result = await debugger.list_globals(pattern="g_retries")
    assert any(e["name"] == "g_retries" for e in result["globals"])


async def test_globals_can_carry_their_current_values(debugger, target_binary):
    await debugger.start(binary=target_binary)
    result = await debugger.list_globals(pattern="g_connection_count",
                                         include_values=True)
    entry = result["globals"][0]
    assert entry["value"] == "0x11"  # 17, in hex


# -- breakpoints and stops ----------------------------------------------------


async def test_breakpoint_on_a_cpp_function(debugger, target_binary):
    await debugger.start(binary=target_binary)
    result = await debugger.set_breakpoint("parse_header")
    # C++ breakpoints report the full demangled signature, not the bare name.
    assert result["breakpoint"]["func"].startswith("parse_header")


async def test_run_stops_at_the_breakpoint_with_context(stopped_in_parse_header):
    status = await stopped_in_parse_header.status()
    assert status["frame"]["func"] == "parse_header"
    assert status["source"], "no source context at the stop"
    assert any(line["current"] for line in status["source"])


async def test_conditional_breakpoint_is_accepted(debugger, target_binary):
    await debugger.start(binary=target_binary)
    result = await debugger.set_breakpoint("parse_header", condition="limit > 0")
    assert result["breakpoint"].get("cond") == "limit > 0"


# -- stack --------------------------------------------------------------------


async def test_backtrace_shows_the_real_call_chain(stopped_in_parse_header):
    result = await stopped_in_parse_header.backtrace(limit=10)
    funcs = [frame["func"] for frame in result["frames"]]
    assert funcs[:3] == ["parse_header", "handle_packet", "main"]


async def test_frame_exposes_locals_and_arguments(stopped_in_parse_header):
    result = await stopped_in_parse_header.select_frame(0)
    names = {v["name"] for v in result["variables"]}
    assert {"raw", "limit", "out"} <= names, names


async def test_selecting_an_outer_frame_changes_scope(stopped_in_parse_header):
    result = await stopped_in_parse_header.select_frame(2)  # main
    assert result["frame"]["func"] == "main"


# -- evaluation, in hex -------------------------------------------------------


async def test_integers_evaluate_in_hex(stopped_in_parse_header):
    """Sessions set output-radix 16 so values agree with memory dumps."""
    result = await stopped_in_parse_header.evaluate("g_connection_count")
    assert result["value"] == "0x11"  # 17


async def test_arguments_evaluate_in_hex(stopped_in_parse_header):
    result = await stopped_in_parse_header.evaluate("limit")
    assert result["value"] == "0x1000"  # 4096


async def test_char_arrays_stay_readable_in_hex_mode(stopped_in_parse_header):
    result = await stopped_in_parse_header.evaluate("g_hostname")
    assert "prod-db-01" in result["value"]


async def test_struct_pointers_dereference(stopped_in_parse_header):
    result = await stopped_in_parse_header.evaluate("*out")
    assert "magic" in result["value"] and "length" in result["value"]


async def test_unknown_symbol_is_a_clear_error(stopped_in_parse_header):
    from gdb_mcp.session import GdbError

    with pytest.raises(GdbError, match="No symbol"):
        await stopped_in_parse_header.evaluate("no_such_symbol_here")


async def test_inferior_function_calls_are_blocked_by_default(
    stopped_in_parse_header,
):
    """`print f()` really runs f() inside the target; that needs opting in."""
    from gdb_mcp.session import GdbError

    with pytest.raises(GdbError):
        await stopped_in_parse_header.evaluate("strlen(raw)")


# -- memory -------------------------------------------------------------------


async def test_memory_dump_reads_a_known_global_array(stopped_in_parse_header):
    result = await stopped_in_parse_header.read_memory(
        "g_table", count=4, word_size=8
    )
    words = [w for row in result["rows"] for w in row["data"]]
    assert words[0] == "0x1111111111111111"
    assert words[3] == "0x4444444444444444"


async def test_memory_dump_ascii_gutter_marks_unprintables(
    stopped_in_parse_header,
):
    """GDB wants the literal replacement char; passing "46" renders '4'."""
    result = await stopped_in_parse_header.read_memory(
        "g_table", count=2, word_size=8
    )
    # 0x11 is unprintable, 0x22 is '"'.
    assert "|........" in result["dump"]
    assert '"' in result["dump"]


async def test_memory_dump_shows_strings_in_the_gutter(stopped_in_parse_header):
    result = await stopped_in_parse_header.read_memory(
        "g_hostname", count=4, word_size=4
    )
    assert "prod-db-01" in result["dump"]
    assert "." in result["dump"]  # trailing NULs


@pytest.mark.parametrize("word_size", WORD_SIZES)
async def test_memory_dump_at_each_word_size(stopped_in_parse_header, word_size):
    result = await stopped_in_parse_header.read_memory(
        "g_table", count=4, word_size=word_size
    )
    assert result["rows"], "no rows returned"
    width = len(result["rows"][0]["data"][0])
    assert width == 2 + word_size * 2, result["rows"][0]["data"][0]


async def test_memory_dump_accepts_a_register_expression(stopped_in_parse_header):
    result = await stopped_in_parse_header.read_memory("$sp", count=2)
    assert result["rows"]


# -- low level ----------------------------------------------------------------


async def test_registers_are_readable(stopped_in_parse_header):
    result = await stopped_in_parse_header.registers(names=["rip", "rsp"])
    assert set(result["registers"]) == {"rip", "rsp"}
    assert result["registers"]["rip"].startswith("0x")


async def test_disassembly_at_the_current_pc(stopped_in_parse_header):
    """The bare `-data-disassemble -- N` form is rejected by modern GDB."""
    result = await stopped_in_parse_header.disassemble(count=5)
    assert len(result["instructions"]) >= 1
    assert "address" in result["instructions"][0]


async def test_disassembly_at_a_named_function(stopped_in_parse_header):
    result = await stopped_in_parse_header.disassemble(location="main", count=5)
    assert result["instructions"]


async def test_source_listing(stopped_in_parse_header):
    result = await stopped_in_parse_header.list_source(lines=6)
    assert result.get("source") or result.get("source_text")


# -- execution ----------------------------------------------------------------


async def test_step_advances_one_line(stopped_in_parse_header):
    before = (await stopped_in_parse_header.status())["frame"]["line"]
    after = await stopped_in_parse_header.step("next", timeout=30)
    assert after["state"] == "stopped"
    assert after["frame"]["line"] != before


async def test_finish_returns_to_the_caller(stopped_in_parse_header):
    result = await stopped_in_parse_header.step("finish", timeout=30)
    assert result["state"] == "stopped"
    assert result["frame"]["func"] == "handle_packet"


async def test_continue_runs_to_exit(stopped_in_parse_header):
    result = await stopped_in_parse_header.cont(timeout=60)
    assert result["state"] == "exited"


async def test_program_stdout_is_captured_through_the_pty(
    stopped_in_parse_header,
):
    result = await stopped_in_parse_header.cont(timeout=60)
    output = result.get("program_output", "")
    assert "total=55" in output


async def test_captured_output_has_no_carriage_returns(stopped_in_parse_header):
    """CRLF is an artefact of the pty, not something the program wrote."""
    result = await stopped_in_parse_header.cont(timeout=60)
    assert "\r" not in result.get("program_output", "")


async def test_draining_output_twice_explains_the_empty_result(
    stopped_in_parse_header,
):
    await stopped_in_parse_header.cont(timeout=60)
    drained = await stopped_in_parse_header.program_output()
    assert drained["output"] == ""
    assert "delivered" in drained["note"]


# -- reverse debugging ---------------------------------------------------------


async def test_reverse_without_recording_gives_a_clear_error(stopped_in_parse_header):
    """The fake proves the wrapping logic; this proves real gdb actually
    raises on a bare `--reverse` and that the wrapped message still matches."""
    from gdb_mcp.session import GdbError

    with pytest.raises(GdbError, match="gdb_record"):
        await stopped_in_parse_header.reverse(kind="step")


async def test_reverse_step_undoes_forward_steps(stopped_in_parse_header):
    """Step forward through parse_header's loop, then step the same distance
    backward -- a real process-record round trip, not just protocol shape."""
    debugger = stopped_in_parse_header
    start_line = (await debugger.status())["frame"]["line"]

    await debugger.record(action="start")
    forward = await debugger.step(kind="next", count=3, timeout=30)
    assert forward["state"] == "stopped"
    assert forward["frame"]["line"] != start_line

    back = await debugger.reverse(kind="step", count=3, timeout=30)
    assert back["state"] == "stopped"
    assert back["frame"]["line"] == start_line


# -- crashes and core dumps ---------------------------------------------------


@pytest.fixture
async def crashed(debugger, target_binary):
    await debugger.start(binary=target_binary, args=["crash"], session="crash")
    stop = await debugger.run(stop_at_main=False, timeout=60, session="crash")
    assert stop["state"] == "stopped", stop
    return debugger, stop


async def test_segfault_is_reported_with_its_signal(crashed):
    _debugger, stop = crashed
    assert stop["reason"] == "signal-received"
    assert stop["signal"] == "SIGSEGV"
    assert "Segmentation fault" in stop["signal_meaning"]


async def test_crash_frame_is_identified(crashed):
    _debugger, stop = crashed
    assert stop["frame"]["func"] == "main"


async def test_core_dump_round_trip(crashed, target_binary, tmp_path):
    """Write a core from the stopped inferior, then post-mortem it."""
    debugger, _stop = crashed
    core = str(tmp_path / "core.target")
    await debugger.raw(f"generate-core-file {core}", session="crash")
    assert os.path.exists(core), "gdb did not write a core file"
    await debugger.stop(session="crash", kill=True)

    # load_core must open its own session rather than demanding gdb_start.
    loaded = await debugger.load_core(target_binary, core, session="post")
    funcs = [frame["func"] for frame in loaded["backtrace"]]
    assert "main" in funcs


async def test_globals_are_readable_from_a_core(crashed, target_binary, tmp_path):
    debugger, _stop = crashed
    core = str(tmp_path / "core2.target")
    await debugger.raw(f"generate-core-file {core}", session="crash")
    await debugger.stop(session="crash", kill=True)

    await debugger.load_core(target_binary, core, session="post")
    value = await debugger.evaluate("g_connection_count", session="post")
    assert value["value"] == "0x11"


async def test_disassembly_is_flat_and_source_annotated(stopped_in_parse_header):
    """GDB groups instructions by source line; count must limit instructions."""
    result = await stopped_in_parse_header.disassemble(count=3)
    assert len(result["instructions"]) == 3
    for insn in result["instructions"]:
        assert "address" in insn
        assert "inst" in insn
        assert insn.get("source_line")
